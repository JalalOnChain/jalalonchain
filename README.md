# JalalOnChain

Live crypto whale-transaction tracker (Bitcoin, Ethereum, and major EVM chains) plus a
daily on-chain/off-chain knowledge briefing, built for traders and learners.

- `index.html` — the site
- `data/transactions.json` — whale moves, refreshed hourly by the GitHub Action
- `data/knowledge.json` — daily briefings, advanced one-per-day from `data/knowledge-pool.json`
- `data/launches.json` — new tokens that crossed $1M market cap (pump.fun + DexScreener across chains, including Robinhood's chain where indexed)
- `data/news.json` — crypto news, hacks, and government/regulatory actions (CoinDesk, The Block, DOJ, SEC, OFAC — Chainalysis and TRM Labs are deliberately excluded)
- `data/twitter-watch.json` — hand-curated list of hack-alert and investigator X/Twitter accounts (not live-synced; X's API doesn't allow free reading, so this is reviewed manually instead)
- `.github/workflows/sync.yml` — the hourly job that keeps everything current
- `scripts/sync.py` — what that job actually runs

Data is synced automatically. Do not hand-edit the `data/` files — they're overwritten
on every sync.

## One-time setup (do this once after uploading)

1. **Add your Etherscan API key as a secret** (needed for the EVM whale data):
   Settings → Secrets and variables → Actions → New repository secret
   Name: `ETHERSCAN_KEY`, Value: your free key from etherscan.io.

2. **Turn on GitHub Pages**:
   Settings → Pages → Build and deployment → Source: "Deploy from a branch" →
   Branch: `main`, folder: `/ (root)` → Save.
   Your site goes live at `https://<your-username>.github.io/jalalonchain/`.

3. **Make sure Actions can push**: Settings → Actions → General → Workflow permissions →
   "Read and write permissions" → Save. (The workflow also requests this itself; only
   change this if the first sync run fails with a permissions error.)

4. Optional: trigger the first sync immediately instead of waiting for the top of the
   hour — Actions tab → "Sync whale data" → Run workflow.

## Adding a custom domain later

Settings → Pages → Custom domain → enter your domain → Save, then add the DNS records
GitHub shows you at your domain registrar. No code changes needed.
