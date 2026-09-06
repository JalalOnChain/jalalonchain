# JalalOnChain

Live crypto whale-transaction tracker (Bitcoin, Ethereum, and major EVM chains) plus a
daily on-chain/off-chain knowledge briefing, built for traders and learners.

- `index.html` — the site
- `data/transactions.json` — whale moves, refreshed roughly hourly
- `data/knowledge.json` — daily briefings

Data is synced by an automated background job. Do not hand-edit the `data/` files;
they are overwritten on every sync.
