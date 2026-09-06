#!/usr/bin/env python3
"""
JalalOnChain hourly data sync.

Runs inside GitHub Actions (real internet access, no external secrets needed
for git itself — GitHub provides GITHUB_TOKEN automatically). Responsibilities:

1. Pull large EVM transfers for a small watchlist of well-known exchange
   wallets via the Etherscan V2 API (one key covers every EVM chain).
2. Best-effort scrape Lookonchain's public whale-alert reporting for
   cross-chain narrative context (BTC, Solana, Hyperliquid, OTC deals, etc).
3. Merge, de-duplicate, cap the transactions feed, and write data/transactions.json.
4. Advance the daily knowledge briefing from a pre-written rotating pool.

Every step is defensive: a failing data source is skipped, never fatal —
the workflow should always leave the repo in a valid, up-to-date state.
"""

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
TX_PATH = os.path.join(DATA_DIR, "transactions.json")
KNOWLEDGE_PATH = os.path.join(DATA_DIR, "knowledge.json")
POOL_PATH = os.path.join(DATA_DIR, "knowledge-pool.json")
STATE_PATH = os.path.join(DATA_DIR, "knowledge-state.json")
NEWS_PATH = os.path.join(DATA_DIR, "news.json")
LAUNCHES_PATH = os.path.join(DATA_DIR, "launches.json")

MIN_USD = 100_000
MAX_TX = 300
MAX_KNOWLEDGE = 120
ETHERSCAN_KEY = os.environ.get("ETHERSCAN_KEY", "").strip()
ETHERSCAN_BASE = "https://api.etherscan.io/v2/api"
UA = "Mozilla/5.0 (compatible; JalalOnChainBot/1.0; +https://github.com/JalalOnChain/jalalonchain)"

# --- EVM watchlist: well-known exchange wallets, expandable over time -----
WATCHLIST = [
    {"chainid": 1, "chain": "Ethereum", "address": "0xf977814e90da44bfa03b6295a0616a897441acec", "label": "Binance"},
    {"chainid": 1, "chain": "Ethereum", "address": "0x71660c4005ba85c37ccec55d0c4493e66fe775d3", "label": "Coinbase"},
]

# Known stablecoin contracts (Ethereum mainnet) we can safely price at $1.
STABLES = {
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
    "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18),
}

STALE_DATE_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b", re.I)
RECENT_RE = re.compile(r"\b(\d+)\s*(minute|minutes|min|hour|hours|hr|hrs)\s+ago\b", re.I)
AMOUNT_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s?(million|billion|m|b|k|thousand)?", re.I)
TOKEN_RE = re.compile(r"\b([A-Z]{2,10})\b")
NOISE_TOKENS = {"USD", "OTC", "DEX", "CEX", "ATH", "APY", "APR", "NFT", "CEO", "COO", "CTO"}


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


def etherscan(params, tries=2):
    params = dict(params)
    params["apikey"] = ETHERSCAN_KEY
    url = ETHERSCAN_BASE + "?" + urlencode(params)
    for attempt in range(tries):
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": UA})
            data = r.json()
            if data.get("status") == "1" and isinstance(data.get("result"), list):
                return data["result"]
            return []
        except Exception:
            time.sleep(1)
    return []


def get_eth_price():
    try:
        # ethprice returns a dict, not a list, so it's called directly rather
        # than through etherscan() (which expects a list result)
        params = {"chainid": 1, "module": "stats", "action": "ethprice", "apikey": ETHERSCAN_KEY}
        url = ETHERSCAN_BASE + "?" + urlencode(params)
        r = requests.get(url, timeout=20, headers={"User-Agent": UA})
        data = r.json()
        if data.get("status") == "1":
            return float(data["result"]["ethusd"])
    except Exception:
        pass
    return None


def fetch_evm_transfers(existing_ids):
    if not ETHERSCAN_KEY:
        print("No ETHERSCAN_KEY set — skipping EVM watchlist sync.")
        return []
    eth_usd = get_eth_price()
    out = []
    for w in WATCHLIST:
        # Native currency transfers
        txs = etherscan({
            "chainid": w["chainid"], "module": "account", "action": "txlist",
            "address": w["address"], "sort": "desc", "page": 1, "offset": 25,
        })
        for tx in txs:
            try:
                if tx.get("isError") == "1":
                    continue
                value_native = int(tx["value"]) / 1e18
                if value_native <= 0 or eth_usd is None:
                    continue
                usd = value_native * eth_usd
                if usd < MIN_USD:
                    continue
                tx_hash = tx["hash"]
                if tx_hash in existing_ids:
                    continue
                to_addr = (tx.get("to") or "").lower()
                from_addr = (tx.get("from") or "").lower()
                if to_addr == w["address"].lower():
                    direction, entity, counterparty = "sell", w["label"], short_addr(from_addr)
                    summary = f"{short_addr(from_addr)} sent {value_native:.3f} ETH into {w['label']} (exchange inflow)"
                else:
                    direction, entity, counterparty = "buy", w["label"], short_addr(to_addr)
                    summary = f"{w['label']} sent {value_native:.3f} ETH out to {short_addr(to_addr)} (exchange outflow)"
                out.append({
                    "id": tx_hash, "token": "ETH", "chain": w["chain"], "amountUsd": round(usd, 2),
                    "amountToken": round(value_native, 4), "direction": direction, "entity": entity,
                    "counterparty": counterparty, "timestamp": iso_from_unix(tx.get("timeStamp")),
                    "fetchedAt": now_iso(), "source": "Etherscan", "sourceUrl": f"https://etherscan.io/tx/{tx_hash}",
                    "summary": summary,
                })
            except Exception:
                continue

        # Stablecoin transfers
        toktxs = etherscan({
            "chainid": w["chainid"], "module": "account", "action": "tokentx",
            "address": w["address"], "sort": "desc", "page": 1, "offset": 40,
        })
        for tx in toktxs:
            try:
                contract = (tx.get("contractAddress") or "").lower()
                if contract not in STABLES:
                    continue
                symbol, decimals = STABLES[contract]
                amount = int(tx["value"]) / (10 ** decimals)
                if amount < MIN_USD:
                    continue
                tx_hash = tx["hash"]
                if tx_hash in existing_ids:
                    continue
                to_addr = (tx.get("to") or "").lower()
                from_addr = (tx.get("from") or "").lower()
                if to_addr == w["address"].lower():
                    direction, entity, counterparty = "sell", w["label"], short_addr(from_addr)
                    summary = f"{short_addr(from_addr)} deposited {amount:,.0f} {symbol} into {w['label']}"
                else:
                    direction, entity, counterparty = "buy", w["label"], short_addr(to_addr)
                    summary = f"{w['label']} sent {amount:,.0f} {symbol} out to {short_addr(to_addr)}"
                out.append({
                    "id": tx_hash, "token": symbol, "chain": w["chain"], "amountUsd": round(amount, 2),
                    "amountToken": round(amount, 2), "direction": direction, "entity": entity,
                    "counterparty": counterparty, "timestamp": iso_from_unix(tx.get("timeStamp")),
                    "fetchedAt": now_iso(), "source": "Etherscan", "sourceUrl": f"https://etherscan.io/tx/{tx_hash}",
                    "summary": summary,
                })
            except Exception:
                continue
    return out


def short_addr(a):
    a = a or ""
    return (a[:6] + "…" + a[-4:]) if len(a) > 12 else a


def iso_from_unix(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return now_iso()


def parse_amount(num_str, unit):
    n = float(num_str.replace(",", ""))
    unit = (unit or "").lower()
    if unit in ("million", "m"):
        n *= 1_000_000
    elif unit in ("billion", "b"):
        n *= 1_000_000_000
    elif unit in ("k", "thousand"):
        n *= 1_000
    return n


def scrape_lookonchain(existing_ids):
    out = []
    try:
        from bs4 import BeautifulSoup
    except Exception:
        print("bs4 not available — skipping Lookonchain scrape.")
        return out

    pages = ["https://www.lookonchain.com/", "https://www.lookonchain.com/feeds"]
    for url in pages:
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": UA})
            soup = BeautifulSoup(r.text, "html.parser")
            text = soup.get_text("\n")
            lines = [l.strip() for l in text.split("\n") if l.strip()]
        except Exception:
            continue

        for line in lines:
            if "$" not in line:
                continue
            if STALE_DATE_RE.search(line) and not RECENT_RE.search(line):
                continue
            if not RECENT_RE.search(line) and not re.search(r"\b(today|yesterday)\b", line, re.I):
                continue
            amt_match = AMOUNT_RE.search(line)
            if not amt_match:
                continue
            usd = parse_amount(amt_match.group(1), amt_match.group(2))
            if usd < MIN_USD:
                continue
            tokens = [t for t in TOKEN_RE.findall(line) if t not in NOISE_TOKENS and len(t) <= 6]
            token = tokens[0] if tokens else "?"
            direction = "other"
            low = line.lower()
            if any(w in low for w in ("bought", "purchased", "accumulat")):
                direction = "buy"
            elif any(w in low for w in ("sold", "dumped", "offloaded")):
                direction = "sell"
            elif any(w in low for w in ("transferred", "moved", "withdrew", "deposited", "sent")):
                direction = "transfer"
            elif any(w in low for w in ("burned",)):
                direction = "burn"
            uid = "lo-" + str(abs(hash((line[:80],))))[:12]
            if uid in existing_ids or token == "?":
                continue
            out.append({
                "id": uid, "token": token, "chain": "Multi-chain", "amountUsd": round(usd, 2),
                "amountToken": None, "direction": direction, "entity": "", "counterparty": "",
                "timestamp": now_iso(), "fetchedAt": now_iso(), "source": "Lookonchain",
                "sourceUrl": url, "summary": line[:220],
            })
    return out


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
MAX_NEWS = 150
NEWS_RSS_SOURCES = [
    {"url": "https://www.justice.gov/news/rss?type=press_release&m=1", "source": "U.S. Dept. of Justice", "category": "enforcement", "filter": True},
    {"url": "https://www.sec.gov/enforcement-litigation/litigation-releases/rss", "source": "U.S. SEC Litigation", "category": "enforcement", "filter": True},
    {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "source": "CoinDesk", "category": "market-news", "filter": True},
    {"url": "https://www.theblock.co/rss.xml", "source": "The Block", "category": "market-news", "filter": False},
    {"url": "https://www.chainalysis.com/blog/feed/", "source": "Chainalysis", "category": "market-news", "filter": False},
    {"url": "https://www.trmlabs.com/resources/blog/rss.xml", "source": "TRM Labs", "category": "market-news", "filter": False},
    {"url": "https://blog.merklescience.com/general/rss.xml", "source": "Merkle Science", "category": "market-news", "filter": False},
]
OFAC_URL = "https://ofac.treasury.gov/recent-actions"
ELLIPTIC_BLOG_URL = "https://www.elliptic.co/blog"
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
        uid = "ofac-" + str(abs(hash((entry["title"][:100],))))[:12]
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
    return items


def scrape_elliptic_blog(existing_ids):
    out = []
    try:
        from bs4 import BeautifulSoup
        r = requests.get(ELLIPTIC_BLOG_URL, timeout=20, headers={"User-Agent": UA})
        soup = BeautifulSoup(r.text, "html.parser")
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/blog/" not in href:
                continue
            if any(seg in href for seg in ("/tag/", "/author/", "/page/")):
                continue
            title = a.get_text(" ", strip=True)
            if not title or len(title) < 15 or len(title) > 200:
                continue
            full_url = href if href.startswith("http") else "https://www.elliptic.co" + href
            if full_url in seen:
                continue
            seen.add(full_url)
            uid = "elliptic-" + str(abs(hash((full_url,))))[:12]
            if uid in existing_ids:
                continue
            out.append({
                "id": uid, "title": title, "source": "Elliptic", "category": "market-news",
                "link": full_url, "publishedAt": now_iso(), "fetchedAt": now_iso(), "summary": "",
            })
            if len(out) >= 10:
                break
    except Exception:
        traceback.print_exc()
    return out


def sync_news():
    current = load_json(NEWS_PATH, {"updatedAt": None, "items": []})
    items = current.get("items", [])
    existing_ids = {n.get("id") for n in items}
    new_items = []

    for cfg in NEWS_RSS_SOURCES:
        try:
            entries = fetch_rss(cfg["url"])
        except Exception:
            traceback.print_exc()
            entries = []
        for e in entries:
            haystack = e["title"] + " " + e["description"]
            if cfg.get("filter") and not is_crypto_relevant(haystack):
                continue
            uid = "news-" + str(abs(hash((cfg["source"], e["title"][:120]))))[:12]
            if uid in existing_ids or uid in {n["id"] for n in new_items}:
                continue
            new_items.append({
                "id": uid, "title": e["title"], "source": cfg["source"],
                "category": cfg["category"], "link": e["link"],
                "publishedAt": parse_rss_date(e["pubDate"]) or now_iso(),
                "fetchedAt": now_iso(), "summary": e["description"][:280],
            })
        time.sleep(0.3)

    try:
        new_items += scrape_ofac(existing_ids | {n["id"] for n in new_items})
    except Exception:
        traceback.print_exc()
    try:
        new_items += scrape_elliptic_blog(existing_ids | {n["id"] for n in new_items})
    except Exception:
        traceback.print_exc()

    items = new_items + items
    items.sort(key=lambda n: n.get("publishedAt") or n.get("fetchedAt") or "", reverse=True)
    items = items[:MAX_NEWS]
    save_json(NEWS_PATH, {"updatedAt": now_iso(), "items": items})
    print(f"news: {len(new_items)} new, {len(items)} total")


# --- New token launches ($1M+ market cap, across platforms) ---------------
MAX_LAUNCHES = 80
LAUNCH_MIN_USD = 1_000_000
LAUNCH_MAX_AGE_DAYS = 30
PUMPFUN_URL = "https://frontend-api-v3.pump.fun/coins?offset=0&limit=50&sort=market_cap&order=DESC&includeNsfw=false"
DEXSCREENER_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_PAIR_URL = "https://api.dexscreener.com/tokens/v1/{chain}/{addresses}"


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
    for p in profiles[:60]:
        chain = p.get("chainId")
        addr = p.get("tokenAddress")
        if not chain or not addr:
            continue
        by_chain.setdefault(chain, []).append(addr)

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
            seen_addr = set()
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
                    out.append({
                        "id": uid, "name": base.get("name") or base.get("symbol") or "Unknown",
                        "symbol": base.get("symbol") or "", "chain": chain_label(chain),
                        "platform": "DexScreener / " + chain_label(chain),
                        "marketCapUsd": round(mc, 2), "createdAt": None,
                        "fetchedAt": now_iso(), "link": pr.get("url") or "", "address": addr,
                    })
                except Exception:
                    continue
            time.sleep(0.2)
    return out


def sync_launches():
    current = load_json(LAUNCHES_PATH, {"updatedAt": None, "items": []})
    items = current.get("items", [])
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

    items = new_items + items
    items.sort(key=lambda l: l.get("marketCapUsd") or 0, reverse=True)
    items = items[:MAX_LAUNCHES]

    save_json(LAUNCHES_PATH, {"updatedAt": now_iso(), "items": items})
    print(f"launches: {len(new_items)} new, {len(items)} total")


def sync_transactions():
    current = load_json(TX_PATH, {"updatedAt": None, "items": []})
    items = current.get("items", [])
    existing_ids = {t.get("id") for t in items}

    new_items = []
    try:
        new_items += fetch_evm_transfers(existing_ids)
    except Exception:
        traceback.print_exc()
    try:
        new_items += scrape_lookonchain(existing_ids | {t["id"] for t in new_items})
    except Exception:
        traceback.print_exc()

    items = new_items + items
    items.sort(key=lambda t: t.get("timestamp") or t.get("fetchedAt") or "", reverse=True)
    items = items[:MAX_TX]

    save_json(TX_PATH, {"updatedAt": now_iso(), "items": items})
    print(f"transactions: {len(new_items)} new, {len(items)} total")


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
    sync_transactions()
    sync_knowledge()
    sync_news()
    sync_launches()


if __name__ == "__main__":
    main()
