# JalalOnChain

New tokens tracked once they cross $1M+ market cap, newly-listed DeFi protocols, large
Hyperliquid trades and open positions, live coin prices, crypto news, and enforcement
alerts — plus a daily on-chain/off-chain knowledge briefing, with clear, informative
reads on how crypto and blockchain actually work.

- `index.html` — the site
- `data/news.json` — crypto market news and U.S. government/regulatory enforcement
  actions, refreshed hourly by the GitHub Action
- `data/launches.json` — new tokens that crossed $1M market cap across chains,
  including Robinhood's chain and Robinhood-branded tokens elsewhere
- `data/prices.json` — a live top-20 coin price snapshot for the homepage price ticker
- `data/defi.json` — recently-listed DeFi protocols/entities by TVL
- `data/hyperliquid.json` — large ($100K+) Hyperliquid perp positions and recent
  large trades; the tracked addresses are re-selected every sync from Hyperliquid's
  current top accounts
- `data/knowledge.json` — daily briefings, advanced one-per-day from `data/knowledge-pool.json`
- `.github/workflows/sync.yml` — the hourly job that keeps everything current
- `scripts/sync.py` — what that job actually runs

Data is synced automatically. Do not hand-edit the `data/` files — they're overwritten
on every sync.

## One-time setup (do this once after uploading)

1. **Turn on GitHub Pages**:
   Settings → Pages → Build and deployment → Source: "Deploy from a branch" →
   Branch: `main`, folder: `/ (root)` → Save.
   Your site goes live at `https://<your-username>.github.io/jalalonchain/`.

2. **Make sure Actions can push**: Settings → Actions → General → Workflow permissions →
   "Read and write permissions" → Save. (The workflow also requests this itself; only
   change this if the first sync run fails with a permissions error.)

3. Optional: trigger the first sync immediately instead of waiting for the top of the
   hour — Actions tab → "Sync site data" → Run workflow.

## Adding a custom domain later

Settings → Pages → Custom domain → enter your domain → Save, then add the DNS records
GitHub shows you at your domain registrar. No code changes needed.
