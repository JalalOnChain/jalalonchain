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


if __name__ == "__main__":
    main()
