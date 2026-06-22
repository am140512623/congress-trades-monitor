# Congress Trades — Weekly Monitor

Pulls U.S. House periodic transaction report (PTR) filings for the most recent
completed week from the official House Clerk financial-disclosure dataset,
flags activity by a watchlist of priority traders, builds a Markdown report,
and delivers it to Telegram.

## Run locally

```bash
# build the report and print it, no Telegram send
python congress_trades.py --no-send

# build and send to Telegram (reads telegram_config.json)
python congress_trades.py

# force a specific week (Monday start)
python congress_trades.py --week 2026-06-15
```

No dependencies — Python 3.10+ standard library only.

### Telegram credentials (local)

`telegram_config.json` (kept out of git via `.gitignore`):

```json
{ "bot_token": "123456:ABC...", "chat_id": "7788611624" }
```

## Deploy to GitHub (runs automatically every Monday)

1. Create a new GitHub repo and push this folder.
   `telegram_config.json` is gitignored, so your token stays local.
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**
   - `TELEGRAM_BOT_TOKEN` — your BotFather token
   - `TELEGRAM_CHAT_ID` — `7788611624`
3. The workflow `.github/workflows/weekly-report.yml` runs every Monday and can
   also be triggered manually from the **Actions** tab (**Run workflow**).
   The script reads the secrets from environment variables in CI.

> GitHub Actions cron is in **UTC**. The workflow is set to `05:04 UTC Monday`
> = **08:04 Qatar time** (UTC+3). Change the `cron:` line for a different time.

## What it does / current limits

- **House PTRs:** filings from the House Clerk FD index; per-trade detail
  (ticker, buy/sell, amount, dates) parsed from each official PTR **PDF**. ✅
- **Senate PTRs:** pulled from `efdsearch.senate.gov`. That site is behind
  Akamai bot protection, so `curl_cffi` (Chrome TLS impersonation) is used to
  accept the agreement and read each electronic filing. ✅
  - Senate **paper** filings are scanned images and cannot be parsed (logged).
  - Note: Akamai may rate-limit/deny datacenter IPs. If the GitHub Actions run
    can't reach the Senate site, the script logs it as a gap and still delivers
    the full House report. Run `--no-senate` to skip Senate intentionally.
- Every trade row includes a **verify** link to its official source.
- Priority-trader watchlist (House + Senate) is in `PRIORITY_TRADERS` in
  `congress_trades.py`. Senators in the list: Tuberville, Whitehouse, McCormick.

### Performance tracker (`portfolio.csv`)

Every **buy** opens a position in `portfolio.csv`, recording the price on the
trade date (`entry_price`) and on the disclosure date (`disclosure_price` — the
earliest a follower could have bought). Each run refreshes `current_price` and
`ret_since_disclosure_pct` for open positions. When the same member later
**sells** that ticker, the matching open position is **closed** and realized
returns are recorded. Prices come from yfinance; Treasuries/CUSIPs and tickers
without price data are skipped.

The GitHub Actions run commits the updated `portfolio.csv` back to the repo each
week, so the history accumulates automatically — just open the file to review it.

### Useful flags

```bash
python congress_trades.py --no-send     # preview only, no Telegram
python congress_trades.py --no-senate   # House only (skip Senate)
python congress_trades.py --no-pdf      # House filing-level only (fast)
python congress_trades.py --no-track    # skip portfolio.csv price tracking
python congress_trades.py --week 2026-06-15
```
