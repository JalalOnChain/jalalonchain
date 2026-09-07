#!/usr/bin/env python3
"""
JalalOnChain hourly data sync.

Runs inside GitHub Actions (real internet access, no external secrets needed
for git itself — GitHub provides GITHUB_TOKEN automatically). Responsibilities:

1. Pull crypto news, hack alerts and government/regulatory enforcement actions
   from RSS feeds and a couple of best-effort scrapes, de-duplicate with
   stable content-hash IDs, and write data/news.json.
2. Pull new token launches that crossed a market-cap threshold from pump.fun
   and DexScreener (including a targeted search for Robinhood-branded/chain
   tokens), and write data/launches.json.
3. Pull a live top-N coin price snapshot from CoinGecko for the homepage
   price ticker, and write data/prices.json.
4. Pull newly-listed DeFi protocols/entities (by TVL) from DefiLlama, and
   write data/defi.json.
5. Pull large ($100K+) Hyperliquid perp positions and recent large trades for
   the current top accounts on Hyperliquid's own public leaderboard/info API
   (not any third-party wallet-tracking product), and write
   data/hyperliquid.json.
6. Advance the daily knowledge briefing from a pre-written rotating pool.

Every step is defensive: a failing data source is skipped, never fatal —
the workflow should always leave the repo in a valid, up-to-date state.

Note on IDs: every item's id is derived from a stable content hash
(hashlib.md5), never Python's built-in hash(), which is randomized per
process (PYTHONHASHSEED) and would make "already seen" de-duplication fail
across separate runs — that was the cause of the duplicate/tripled entries
seen in earlier versions of this script.
"""

import hashlib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, quote

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
KNOWLEDGE_PATH = os.path.join(DATA_DIR, "knowledge.json")
POOL_PATH = os.path.join(DATA_DIR, "knowledge-pool.json")
STATE_PATH = os.path.join(DATA_DIR, "knowledge-state.json")
NEWS_PATH = os.path.join(DATA_DIR, "news.json")
LAUNCHES_PATH = os.path.join(DATA_DIR, "launches.json")
PRICES_PATH = os.path.join(DATA_DIR, "prices.json")
DEFI_PATH = os.path.join(DATA_DIR, "defi.json")
HYPERLIQUID_PATH = os.path.join(DATA_DIR, "hyperliquid.json")

MAX_KNOWLEDGE = 120
UA = "Mozilla/5.0 (compatible; JalalOnChainBot/1.0; +https://github.com/JalalOnChain/jalalonchain)"

STALE_DATE_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b", re.I)
AMOUNT_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s?(million|billion|m|b|k|thousand)?", re.I)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


def stable_id(prefix, *parts):
    """Deterministic short id from stable content — safe across separate
    process runs, unlike Python's randomized built-in hash()."""
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return prefix + "-" + hashlib.md5(raw).hexdigest()[:12]


def chain_label(c):
    return CHAIN_LABELS.get(c, c.capitalize() if c else "Unknown")


CHAIN_LABELS = {
    "solana": "Solana", "ethereum": "Ethereum", "bsc": "BNB Chain", "base": "Base",
    "robinhood": "Robinhood Chain", "polygon": "Polygon", "arbitrum": "Arbitrum",
    "avalanche": "Avalanche", "blast": "Blast", "sui": "Sui", "ton": "TON",
    "tron": "Tron", "optimism": "Optimism",
}

# --- News & enforcement ------------------------------------------------
# Twitter/X isn't pulled live here: X's API no longer allows free automated
# reading of tweets, and scraping it without login is against their ToS and
# unreliable — so real-time hack/investigator accounts are hand-curated in
# data/twitter-watch.json instead of synced.
#
# CoinDesk and The Block were dropped (too high-frequency/generic), and the
# blockchain-intel firm blogs (Chainalysis, TRM Labs, Merkle Science,
# Elliptic) were dropped per request — their posts were stale/not needed.
# REMOVED_NEWS_SOURCES actively purges any of those already sitting in the
# saved data file, not just future fetches, so old items from them disappear
# on the next sync too. Decrypt fills the coin-specific/market-news slot —
# moderate frequency, and it covers non-crypto tech stories too (it's a
# broader tech-and-crypto outlet), so filter=True keeps only the
# crypto/blockchain-relevant articles from it.
MAX_NEWS = 150
MAX_PER_SOURCE_PER_RUN = 4  # anti-spam cap: at most this many new items per source per sync
NEWS_RSS_SOURCES = [
    {"url": "https://www.justice.gov/news/rss?type=press_release&m=1", "source": "U.S. Dept. of Justice", "category": "enforcement", "filter": True},
    {"url": "https://www.sec.gov/enforcement-litigation/litigation-releases/rss", "source": "U.S. SEC Litigation", "category": "enforcement", "filter": True},
    {"url": "https://decrypt.co/feed", "source": "Decrypt", "category": "market-news", "filter": True},
]
REMOVED_NEWS_SOURCES = {"Chainalysis", "TRM Labs", "Merkle Science", "Elliptic", "The Block", "CoinDesk"}
OFAC_URL = "https://ofac.treasury.gov/recent-actions"
CRYPTO_KEYWORDS = [
    "crypto", "cryptocurrency", "bitcoin", "ether", "ethereum", "blockchain",
    "digital asset", "digital currency", "virtual currency", "virtual asset",
    "stablecoin", "defi", "nft", "token", "wallet", "smart contract", "web3",
    "coin offering", "usdt", "usdc", "binance", "coinbase", "solana",
    "hyperliquid", "mining", "exchange hack", "seized", "seizure",
]
MONTHS_RE = r"(January|February|March|April|May|June|July|August|September|October|November|December)"


def is_crypto_relevant(text):
    low = (text or "").lower()
    return any(k in low for k in CRYPTO_KEYWORDS)


TITLE_NORM_RE = re.compile(r"[^a-z0-9]+")


def news_dedupe_key(item):
    """Content-based key (source + normalized title), independent of id.

    Older data written before ids were made deterministic can contain the
    same headline stored multiple times under different ids — this key
    lets a single pass collapse those regardless of when they were saved.
    """
    title_norm = TITLE_NORM_RE.sub(" ", (item.get("title") or "").lower()).strip()[:140]
    return (item.get("source") or "", title_norm)


def dedupe_news(items):
    seen = set()
    out = []
    for it in items:
        key = news_dedupe_key(it)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def strip_html(s):
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(s or "", "html.parser").get_text(" ").strip()
    except Exception:
        return re.sub(r"<[^>]+>", " ", s or "").strip()


def parse_rss_date(s):
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def fetch_rss(url):
    import xml.etree.ElementTree as ET
    out = []
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml",
        })
        root = ET.fromstring(r.content)
        for item in root.iter("item"):
            def get(tag):
                el = item.find(tag)
                return el.text if el is not None and el.text else ""
            title = get("title").strip()
            link = get("link").strip()
            pub = get("pubDate").strip()
            desc = strip_html(get("description"))[:400]
            if not title:
                continue
            out.append({"title": title, "link": link, "pubDate": pub, "description": desc})
    except Exception:
        traceback.print_exc()
    return out


def scrape_ofac(existing_ids):
    entries = []
    try:
        from bs4 import BeautifulSoup
        r = requests.get(OFAC_URL, timeout=20, headers={"User-Agent": UA})
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text("\n")
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        date_re = re.compile(r"^" + MONTHS_RE + r"\s+\d{1,2},\s*\d{4}$")
        i = 0
        while i < len(lines):
            if date_re.match(lines[i]):
                date_str = lines[i]
                title = lines[i + 1] if i + 1 < len(lines) else ""
                if title and not date_re.match(title):
                    entries.append({"date": date_str, "title": title})
                i += 2
            else:
                i += 1
    except Exception:
        traceback.print_exc()

    items = []
    for entry in entries[:20]:
        if not is_crypto_relevant(entry["title"]):
            continue
        uid = stable_id("ofac", entry["title"][:100])
        if uid in existing_ids:
            continue
        try:
            dt = datetime.strptime(entry["date"], "%B %d, %Y").replace(tzinfo=timezone.utc)
            iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            iso = now_iso()
        items.append({
            "id": uid, "title": entry["title"], "source": "OFAC (U.S. Treasury)",
            "category": "enforcement", "link": OFAC_URL, "publishedAt": iso,
            "fetchedAt": now_iso(), "summary": "Sanctions action listed on OFAC's recent actions page.",
        })
        if len(items) >= MAX_PER_SOURCE_PER_RUN:
            break
    return items


BLOCKBEATS_URL = "https://api.theblockbeats.news/v2/rss/newsflash"


def parse_blockbeats_date(s):
    d = parse_rss_date(s)
    if d:
        return d
    try:
        dt = datetime.strptime((s or "").strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def fetch_blockbeats_news(existing_ids):
    """BlockBeats' public newsflash feed, English. Their own docs describe this
    endpoint as RSS/XML selected via a "language" header, but the same URL has
    also been observed returning a JSON envelope instead — this sandbox can't
    reach the host directly to confirm which one GitHub's runner will actually
    see, so both response shapes are handled defensively. A parsing miss here
    just means 0 BlockBeats items for this run, not a broken sync — every other
    source is untouched either way."""
    entries = []
    try:
        r = requests.get(BLOCKBEATS_URL, timeout=20, headers={
            "User-Agent": UA, "language": "en",
            "Accept": "application/rss+xml, application/xml, text/xml, application/json",
        })
        ctype = (r.headers.get("content-type") or "").lower()
        if "json" in ctype:
            data = r.json()
            rows = data.get("data") if isinstance(data, dict) else data
            if isinstance(rows, dict):
                rows = rows.get("list") or rows.get("items") or []
            for row in (rows or []):
                if not isinstance(row, dict):
                    continue
                title = (row.get("title") or "").strip()
                if not title:
                    continue
                entries.append({
                    "title": title,
                    "link": row.get("link") or row.get("url") or "",
                    "pubDate": row.get("create_time") or row.get("pub_time") or row.get("published_at") or "",
                    "description": strip_html(row.get("content") or row.get("description") or "")[:400],
                })
        else:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(r.content)
            for item in root.iter("item"):
                def get(tag):
                    el = item.find(tag)
                    return el.text if el is not None and el.text else ""
                title = get("title").strip()
                if not title:
                    continue
                entries.append({
                    "title": title, "link": get("link").strip(),
                    "pubDate": get("pubDate").strip(),
                    "description": strip_html(get("description"))[:400],
                })
    except Exception:
        traceback.print_exc()

    items = []
    for e in entries:
        uid = stable_id("news", "BlockBeats", e["title"][:120])
        if uid in existing_ids:
            continue
        items.append({
            "id": uid, "title": e["title"], "source": "BlockBeats",
            "category": "market-news", "link": e["link"],
            "publishedAt": parse_blockbeats_date(e["pubDate"]) or now_iso(),
            "fetchedAt": now_iso(), "summary": e["description"][:280],
        })
        if len(items) >= MAX_PER_SOURCE_PER_RUN:
            break
    return items


def sync_news():
    current = load_json(NEWS_PATH, {"updatedAt": None, "items": []})
    items = current.get("items", [])
    # Actively purge any already-saved items from sources we no longer want,
    # not just skip fetching new ones from them.
    purged = len(items)
    items = [n for n in items if n.get("source") not in REMOVED_NEWS_SOURCES]
    purged -= len(items)
    if purged:
        print(f"news: purged {purged} item(s) from removed sources")
    existing_ids = {n.get("id") for n in items}
    new_items = []

    for cfg in NEWS_RSS_SOURCES:
        try:
            entries = fetch_rss(cfg["url"])
        except Exception:
            traceback.print_exc()
            entries = []
        added_for_source = 0
        for e in entries:
            if added_for_source >= MAX_PER_SOURCE_PER_RUN:
                break
            haystack = e["title"] + " " + e["description"]
            if cfg.get("filter") and not is_crypto_relevant(haystack):
                continue
            uid = stable_id("news", cfg["source"], e["title"][:120])
            if uid in existing_ids or uid in {n["id"] for n in new_items}:
                continue
            new_items.append({
                "id": uid, "title": e["title"], "source": cfg["source"],
                "category": cfg["category"], "link": e["link"],
                "publishedAt": parse_rss_date(e["pubDate"]) or now_iso(),
                "fetchedAt": now_iso(), "summary": e["description"][:280],
            })
            added_for_source += 1
        time.sleep(0.3)

    try:
        new_items += fetch_blockbeats_news(existing_ids | {n["id"] for n in new_items})
    except Exception:
        traceback.print_exc()

    try:
        new_items += scrape_ofac(existing_ids | {n["id"] for n in new_items})
    except Exception:
        traceback.print_exc()

    items = new_items + items
    items.sort(key=lambda n: n.get("publishedAt") or n.get("fetchedAt") or "", reverse=True)
    before = len(items)
    items = dedupe_news(items)
    items = items[:MAX_NEWS]
    save_json(NEWS_PATH, {"updatedAt": now_iso(), "items": items})
    print(f"news: {len(new_items)} new, {before - len(items)} duplicate(s) collapsed, {len(items)} total")


# --- New token launches ($1M+ market cap, across platforms) ---------------
# Note: crossing $1M once doesn't mean staying there — pump.fun tokens in
# particular routinely crash back to near-zero within hours. Earlier versions
# of this script only ever ADDED items and never revisited them, so a coin
# that peaked at $1M+ and then collapsed kept showing its stale peak value
# forever. sync_launches() now refreshes (or drops) every already-saved item
# every run, in addition to discovering new ones.
MAX_LAUNCHES = 80
LAUNCH_MIN_USD = 1_000_000
LAUNCH_MAX_AGE_DAYS = 30
DELIST_BELOW_USD = 300_000   # confirmed current cap below this -> drop it (well under the $1M entry bar, so it doesn't flicker in/out right at the boundary)
LAUNCH_STALE_MAX_DAYS = 14   # hard cap for items we couldn't actively re-confirm (e.g. no chainId on record)
PUMPFUN_URL = "https://frontend-api-v3.pump.fun/coins?offset=0&limit=50&sort=market_cap&order=DESC&includeNsfw=false"
PUMPFUN_COIN_URL = "https://frontend-api-v3.pump.fun/coins/{mint}"
DEXSCREENER_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_PAIR_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{addresses}"
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={query}"
# Targeted searches to widen coverage beyond whatever happens to be in the
# latest-profiles feed — in particular Robinhood's tokenized-stock/crypto
# offerings, which are easy to miss since they don't always show up as
# freshly "submitted profiles".
DEXSCREENER_SEARCH_QUERIES = ["robinhood"]


def refresh_pumpfun_items(items):
    """Re-check each saved pump.fun token's current market cap. Confirmed
    crashes (or a 404 — the coin's gone) get dropped; a request that just
    fails (timeout, etc) leaves the old record untouched rather than guessing."""
    kept = []
    dropped = 0
    for it in items:
        mint = it.get("address")
        if not mint:
            kept.append(it)
            continue
        try:
            r = requests.get(PUMPFUN_COIN_URL.format(mint=mint), timeout=15, headers={"User-Agent": UA})
            if r.status_code == 404:
                dropped += 1
                time.sleep(0.1)
                continue
            data = r.json()
            mc = float(data.get("usd_market_cap") or data.get("market_cap_usd") or 0)
            if mc <= 0:
                kept.append(it)  # couldn't confirm a live figure — keep the old value
            elif mc < DELIST_BELOW_USD:
                dropped += 1
            else:
                it = dict(it)
                it["marketCapUsd"] = round(mc, 2)
                it["fetchedAt"] = now_iso()
                kept.append(it)
        except Exception:
            kept.append(it)
        time.sleep(0.1)
    if dropped:
        print(f"launches: {dropped} pump.fun token(s) dropped (crashed below ${DELIST_BELOW_USD:,})")
    return kept


def refresh_dexscreener_items(items):
    """Re-check each saved DexScreener-sourced token's current market cap,
    batched by chain. Confirmed crashes get dropped; anything DexScreener
    didn't answer for this run (network hiccup, or genuinely delisted) keeps
    its last-known record — the staleness cutoff in sync_launches() clears
    it out if that persists for too long."""
    by_chain = {}
    kept = []
    for it in items:
        chain, addr = it.get("chainId"), it.get("address")
        if chain and addr:
            by_chain.setdefault(chain, {})[addr] = it
        else:
            kept.append(it)  # no chainId on record (older item) — can't batch-refresh this one

    crashed = 0
    for chain, by_addr in by_chain.items():
        addrs = list(by_addr.keys())
        confirmed = set()
        for i in range(0, len(addrs), 20):
            batch = addrs[i:i + 20]
            url = DEXSCREENER_PAIR_URL.format(chain=chain, addresses=",".join(batch))
            try:
                r = requests.get(url, timeout=20, headers={"User-Agent": UA})
                pairs = r.json()
                if not isinstance(pairs, list):
                    continue
            except Exception:
                continue  # request failed — leave these addresses unconfirmed, not dropped
            for pr in pairs:
                try:
                    base = pr.get("baseToken") or {}
                    addr = (base.get("address") or "").lower()
                    if addr not in by_addr:
                        continue
                    confirmed.add(addr)
                    mc = float(pr.get("marketCap") or pr.get("fdv") or 0)
                    if mc < DELIST_BELOW_USD:
                        crashed += 1
                        continue  # confirmed crash — drop it
                    it = dict(by_addr[addr])
                    it["marketCapUsd"] = round(mc, 2)
                    it["fetchedAt"] = now_iso()
                    kept.append(it)
                except Exception:
                    continue
            time.sleep(0.2)
        for addr, it in by_addr.items():
            if addr not in confirmed:
                kept.append(it)  # unconfirmed this run — keep as-is, let the age cutoff handle it eventually
    if crashed:
        print(f"launches: {crashed} DexScreener token(s) dropped (crashed below ${DELIST_BELOW_USD:,})")
    return kept


def fetch_pumpfun_launches(existing_ids):
    out = []
    try:
        r = requests.get(PUMPFUN_URL, timeout=20, headers={"User-Agent": UA})
        coins = r.json()
        if not isinstance(coins, list):
            return out
        cutoff = datetime.now(timezone.utc) - timedelta(days=LAUNCH_MAX_AGE_DAYS)
        for c in coins:
            try:
                mc = float(c.get("usd_market_cap") or c.get("market_cap_usd") or 0)
                if mc < LAUNCH_MIN_USD:
                    continue
                created_ms = c.get("created_timestamp")
                created_dt = None
                if created_ms:
                    created_dt = datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc)
                    if created_dt < cutoff:
                        continue
                mint = c.get("mint")
                if not mint or ("pf-" + mint) in existing_ids:
                    continue
                out.append({
                    "id": "pf-" + mint, "name": c.get("name") or c.get("symbol") or "Unknown",
                    "symbol": c.get("symbol") or "", "chain": "Solana", "platform": "pump.fun",
                    "marketCapUsd": round(mc, 2),
                    "createdAt": created_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if created_dt else None,
                    "fetchedAt": now_iso(), "link": f"https://pump.fun/{mint}", "address": mint,
                })
            except Exception:
                continue
    except Exception:
        traceback.print_exc()
    return out


def _pairs_to_launch_items(pairs, existing_ids, seen_addr):
    out = []
    for pr in pairs:
        try:
            base = pr.get("baseToken") or {}
            addr = (base.get("address") or "").lower()
            if not addr or addr in seen_addr:
                continue
            mc = float(pr.get("marketCap") or pr.get("fdv") or 0)
            uid = "ds-" + addr
            if mc < LAUNCH_MIN_USD or uid in existing_ids:
                continue
            seen_addr.add(addr)
            chain = pr.get("chainId") or ""
            out.append({
                "id": uid, "name": base.get("name") or base.get("symbol") or "Unknown",
                "symbol": base.get("symbol") or "", "chain": chain_label(chain), "chainId": chain,
                "platform": "DexScreener / " + chain_label(chain),
                "marketCapUsd": round(mc, 2), "createdAt": None,
                "fetchedAt": now_iso(), "link": pr.get("url") or "", "address": addr,
            })
        except Exception:
            continue
    return out


def fetch_dexscreener_launches(existing_ids):
    out = []
    try:
        r = requests.get(DEXSCREENER_PROFILES_URL, timeout=20, headers={"User-Agent": UA})
        profiles = r.json()
        if not isinstance(profiles, list):
            return out
    except Exception:
        traceback.print_exc()
        return out

    # Group latest-profile addresses by chain so the market-cap lookup can be
    # batched (DexScreener accepts comma-separated addresses per call). This
    # naturally covers new launches across whatever platforms/chains
    # DexScreener has indexed — pump.fun clones, Robinhood's tokenized-asset
    # chain, Base/Solana launchpads, etc — not just one named platform.
    by_chain = {}
    for p in profiles[:120]:
        chain = p.get("chainId")
        addr = p.get("tokenAddress")
        if not chain or not addr:
            continue
        by_chain.setdefault(chain, []).append(addr)

    seen_addr = set()
    for chain, addrs in by_chain.items():
        for i in range(0, len(addrs), 20):
            batch = addrs[i:i + 20]
            url = DEXSCREENER_PAIR_URL.format(chain=chain, addresses=",".join(batch))
            try:
                r = requests.get(url, timeout=20, headers={"User-Agent": UA})
                pairs = r.json()
                if not isinstance(pairs, list):
                    continue
            except Exception:
                continue
            out += _pairs_to_launch_items(pairs, existing_ids, seen_addr)
            time.sleep(0.2)
    return out


def fetch_dexscreener_search(query, existing_ids):
    out = []
    try:
        url = DEXSCREENER_SEARCH_URL.format(query=quote(query))
        r = requests.get(url, timeout=20, headers={"User-Agent": UA})
        data = r.json()
        pairs = data.get("pairs") or []
    except Exception:
        traceback.print_exc()
        return out
    return _pairs_to_launch_items(pairs, existing_ids, set())


def sync_launches():
    current = load_json(LAUNCHES_PATH, {"updatedAt": None, "items": []})
    items = current.get("items", [])

    # Re-check every already-saved item before adding anything new — a coin
    # that crossed $1M once and then crashed shouldn't keep showing its stale
    # peak value forever (see the module comment above LAUNCH_MIN_USD).
    pf_items, ds_items, other_items = [], [], []
    for l in items:
        lid = str(l.get("id") or "")
        if lid.startswith("pf-"):
            pf_items.append(l)
        elif lid.startswith("ds-"):
            ds_items.append(l)
        else:
            other_items.append(l)
    try:
        pf_items = refresh_pumpfun_items(pf_items)
    except Exception:
        traceback.print_exc()
    try:
        ds_items = refresh_dexscreener_items(ds_items)
    except Exception:
        traceback.print_exc()
    items = pf_items + ds_items + other_items

    # Safety-net cutoff for anything we haven't been able to actively
    # re-confirm in a long time (e.g. an older DexScreener item saved before
    # chainId was recorded, so it can't be batch-refreshed).
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=LAUNCH_STALE_MAX_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    before_stale = len(items)
    items = [l for l in items if (l.get("fetchedAt") or "") >= stale_cutoff]
    if before_stale - len(items):
        print(f"launches: {before_stale - len(items)} item(s) dropped for staleness (unconfirmed {LAUNCH_STALE_MAX_DAYS}+ days)")

    existing_ids = {l.get("id") for l in items}
    new_items = []
    try:
        new_items += fetch_pumpfun_launches(existing_ids)
    except Exception:
        traceback.print_exc()
    try:
        new_items += fetch_dexscreener_launches(existing_ids | {l["id"] for l in new_items})
    except Exception:
        traceback.print_exc()
    for q in DEXSCREENER_SEARCH_QUERIES:
        try:
            new_items += fetch_dexscreener_search(q, existing_ids | {l["id"] for l in new_items})
        except Exception:
            traceback.print_exc()
        time.sleep(0.2)

    items = new_items + items
    items.sort(key=lambda l: l.get("marketCapUsd") or 0, reverse=True)
    items = items[:MAX_LAUNCHES]

    save_json(LAUNCHES_PATH, {"updatedAt": now_iso(), "items": items})
    print(f"launches: {len(new_items)} new, {len(items)} total")


# --- Live coin price ticker (top coins by market cap) ---------------------
COINGECKO_MARKETS_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&order=market_cap_desc&per_page=20&page=1"
    "&sparkline=false&price_change_percentage=24h"
)


def fetch_prices():
    try:
        r = requests.get(COINGECKO_MARKETS_URL, timeout=20, headers={"User-Agent": UA, "Accept": "application/json"})
        data = r.json()
        if not isinstance(data, list):
            return []
        items = []
        for c in data:
            try:
                items.append({
                    "symbol": (c.get("symbol") or "").upper(),
                    "name": c.get("name") or "",
                    "price": c.get("current_price"),
                    "change24h": c.get("price_change_percentage_24h"),
                    "marketCapRank": c.get("market_cap_rank"),
                })
            except Exception:
                continue
        return items
    except Exception:
        traceback.print_exc()
        return []


def sync_prices():
    items = fetch_prices()
    if not items:
        print("prices: fetch failed or returned nothing — keeping previous snapshot")
        return
    save_json(PRICES_PATH, {"updatedAt": now_iso(), "items": items})
    print(f"prices: {len(items)} coins")


# --- New DeFi protocols/entities (by TVL, recently listed) ----------------
# DefiLlama's public protocols endpoint — no key needed. Each protocol has a
# "listedAt" unix-seconds field when DefiLlama has recorded a listing date;
# we surface the newest-listed ones above a small TVL floor so the feed
# skips dead-on-arrival forks and shows things that actually gained traction.
DEFILLAMA_PROTOCOLS_URL = "https://api.llama.fi/protocols"
MAX_DEFI = 40
DEFI_MIN_TVL = 100_000
DEFI_MAX_AGE_DAYS = 90


def fetch_new_defi_protocols():
    out = []
    try:
        r = requests.get(DEFILLAMA_PROTOCOLS_URL, timeout=25, headers={"User-Agent": UA, "Accept": "application/json"})
        protocols = r.json()
        if not isinstance(protocols, list):
            return out
    except Exception:
        traceback.print_exc()
        return out

    cutoff = datetime.now(timezone.utc) - timedelta(days=DEFI_MAX_AGE_DAYS)
    for p in protocols:
        try:
            listed_at = p.get("listedAt")
            if not listed_at:
                continue
            listed_dt = datetime.fromtimestamp(int(listed_at), tz=timezone.utc)
            if listed_dt < cutoff:
                continue
            tvl = float(p.get("tvl") or 0)
            if tvl < DEFI_MIN_TVL:
                continue
            slug = p.get("slug") or p.get("name")
            uid = stable_id("defi", slug)
            chains = p.get("chains") or ([p["chain"]] if p.get("chain") else [])
            out.append({
                "id": uid, "name": p.get("name") or "Unknown", "symbol": p.get("symbol") or "",
                "category": p.get("category") or "", "chains": chains[:4],
                "tvlUsd": round(tvl, 2), "change1d": p.get("change_1d"),
                "listedAt": listed_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fetchedAt": now_iso(),
                "link": p.get("url") or (f"https://defillama.com/protocol/{slug}" if slug else ""),
            })
        except Exception:
            continue
    return out


def sync_defi():
    fetched = fetch_new_defi_protocols()
    if not fetched:
        print("defi: fetch failed or returned nothing — keeping previous snapshot")
        return
    by_id = {}
    for p in fetched:
        by_id[p["id"]] = p  # de-dupe by protocol, latest wins
    items = sorted(by_id.values(), key=lambda p: p.get("tvlUsd") or 0, reverse=True)[:MAX_DEFI]
    save_json(DEFI_PATH, {"updatedAt": now_iso(), "items": items})
    print(f"defi: {len(items)} newly-listed protocols")


# --- Hyperliquid whale positions & activity ($100K+) ----------------------
# Sourced directly from Hyperliquid's own public, unauthenticated endpoints —
# the same ones the Hyperliquid app itself calls from the browser — never
# from a third-party wallet-tracking product's private backend. The address
# pool isn't a fixed hand-picked watchlist: it's re-derived from Hyperliquid's
# public leaderboard every run, so it naturally rotates as accounts move up
# or down in size.
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
HL_MIN_USD = 100_000
HL_POOL_SIZE = 40          # how many top-by-account-value addresses to scan per run
HL_MAX_POSITIONS = 60
HL_MAX_ACTIVITY = 60
HL_FILL_LOOKBACK_HOURS = 6  # only report trades from roughly the last sync window
HL_STABLES = {"USDC", "USDT0", "USDH", "USDE"}


def hl_info(payload):
    try:
        r = requests.post(HL_INFO_URL, json=payload, timeout=20, headers={"User-Agent": UA})
        return r.json()
    except Exception:
        return None


def fetch_hl_top_addresses(n):
    try:
        r = requests.get(HL_LEADERBOARD_URL, timeout=60, headers={"User-Agent": UA})
        rows = r.json().get("leaderboardRows") or []
    except Exception:
        traceback.print_exc()
        return []
    ranked = []
    for row in rows:
        try:
            addr = row.get("ethAddress")
            av = float(row.get("accountValue") or 0)
            if addr:
                ranked.append((av, addr))
        except Exception:
            continue
    ranked.sort(key=lambda t: t[0], reverse=True)
    return [addr for _, addr in ranked[:n]]


def fetch_hl_positions_for(address):
    out = []
    data = hl_info({"type": "clearinghouseState", "user": address})
    if not data:
        return out
    for ap in data.get("assetPositions") or []:
        try:
            pos = ap.get("position") or {}
            value = float(pos.get("positionValue") or 0)
            if value < HL_MIN_USD:
                continue
            szi = float(pos.get("szi") or 0)
            out.append({
                "id": stable_id("hlpos", address, pos.get("coin")),
                "address": address, "coin": pos.get("coin"),
                "side": "long" if szi >= 0 else "short",
                "size": abs(szi), "positionValueUsd": round(value, 2),
                "entryPx": pos.get("entryPx"), "unrealizedPnlUsd": round(float(pos.get("unrealizedPnl") or 0), 2),
                "leverage": (pos.get("leverage") or {}).get("value"),
                "kind": "perp", "fetchedAt": now_iso(),
                "link": f"https://hypurrscan.io/address/{address}",
            })
        except Exception:
            continue

    spot = hl_info({"type": "spotClearinghouseState", "user": address})
    if spot:
        try:
            stable_total = 0.0
            for bal in spot.get("balances") or []:
                if bal.get("coin") in HL_STABLES:
                    stable_total += float(bal.get("total") or 0)
            if stable_total >= HL_MIN_USD:
                out.append({
                    "id": stable_id("hlpos", address, "spot-stables"),
                    "address": address, "coin": "USDC-equiv",
                    "side": "spot", "size": round(stable_total, 2),
                    "positionValueUsd": round(stable_total, 2),
                    "entryPx": None, "unrealizedPnlUsd": None, "leverage": None,
                    "kind": "spot", "fetchedAt": now_iso(),
                    "link": f"https://hypurrscan.io/address/{address}",
                })
        except Exception:
            pass
    return out


def fetch_hl_activity_for(address, cutoff_ms):
    out = []
    fills = hl_info({"type": "userFills", "user": address})
    if not isinstance(fills, list):
        return out
    for f in fills:
        try:
            t = int(f.get("time") or 0)
            if t < cutoff_ms:
                continue
            px = float(f.get("px") or 0)
            sz = float(f.get("sz") or 0)
            notional = px * sz
            if notional < HL_MIN_USD:
                continue
            out.append({
                "id": "hlfill-" + str(f.get("hash") or f.get("tid")),
                "address": address, "coin": f.get("coin"), "dir": f.get("dir") or f.get("side"),
                "size": sz, "price": px, "notionalUsd": round(notional, 2),
                "closedPnlUsd": round(float(f.get("closedPnl") or 0), 2),
                "time": datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fetchedAt": now_iso(), "link": f"https://hypurrscan.io/address/{address}",
            })
        except Exception:
            continue
    return out


def sync_hyperliquid():
    addresses = fetch_hl_top_addresses(HL_POOL_SIZE)
    if not addresses:
        print("hyperliquid: leaderboard fetch failed — keeping previous snapshot")
        return
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(hours=HL_FILL_LOOKBACK_HOURS)).timestamp() * 1000)

    positions, activity = [], []
    for addr in addresses:
        try:
            positions += fetch_hl_positions_for(addr)
        except Exception:
            traceback.print_exc()
        try:
            activity += fetch_hl_activity_for(addr, cutoff_ms)
        except Exception:
            traceback.print_exc()
        time.sleep(0.15)

    positions.sort(key=lambda p: p.get("positionValueUsd") or 0, reverse=True)
    positions = positions[:HL_MAX_POSITIONS]
    activity_by_id = {a["id"]: a for a in activity}
    activity = sorted(activity_by_id.values(), key=lambda a: a.get("time") or "", reverse=True)[:HL_MAX_ACTIVITY]

    save_json(HYPERLIQUID_PATH, {"updatedAt": now_iso(), "positions": positions, "activity": activity})
    print(f"hyperliquid: {len(addresses)} addresses scanned, {len(positions)} positions, {len(activity)} large trades")


def sync_knowledge():
    knowledge = load_json(KNOWLEDGE_PATH, {"updatedAt": None, "items": []})
    pool = load_json(POOL_PATH, {"items": []}).get("items", [])
    state = load_json(STATE_PATH, {"nextIndex": 0})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    items = knowledge.get("items", [])
    if not any(it.get("id") == today for it in items) and pool:
        idx = state.get("nextIndex", 0) % len(pool)
        entry = pool[idx]
        items.insert(0, {
            "id": today, "title": entry["title"], "body": entry["body"],
            "tags": entry.get("tags", []), "createdAt": now_iso(),
        })
        items = items[:MAX_KNOWLEDGE]
        state["nextIndex"] = idx + 1
        save_json(STATE_PATH, state)
        print(f"knowledge: published '{entry['title']}' for {today}")
    else:
        print("knowledge: today already covered")

    save_json(KNOWLEDGE_PATH, {"updatedAt": now_iso(), "items": items})


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    sync_knowledge()
    sync_news()
    sync_launches()
    sync_prices()
    sync_defi()
    sync_hyperliquid()


if __name__ == "__main__":
    main()
