"""
Performance tracker for congressional BUY trades.

Maintains a persistent `portfolio.csv`:
  - every new BUY opens a position (records entry price on the trade date and
    the disclosure-day price - the earliest a follower could have bought);
  - when the same member later SELLS that ticker, the matching open position is
    CLOSED and realized returns are recorded;
  - open positions get their current price + return refreshed every run.

Prices come from yfinance (free, no API key). Tickers without price data
(Treasuries, CUSIPs, some funds) are skipped and logged.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path

try:
    import yfinance as yf
    _CACHE_DIR = Path(__file__).resolve().parent / "_yfcache"
    _CACHE_DIR.mkdir(exist_ok=True)
    yf.set_tz_cache_location(str(_CACHE_DIR))
    HAS_YF = True
except Exception:  # noqa: BLE001
    HAS_YF = False

FIELDNAMES = [
    "id", "filer", "chamber", "ticker", "company",
    "trade_date", "disclosure_date", "amount",
    "entry_price", "disclosure_price", "status",
    "sell_trade_date", "sell_disclosure_date", "sell_price",
    "current_price", "current_date",
    "ret_since_disclosure_pct", "ret_since_entry_pct",
    "realized_disclosure_pct", "realized_entry_pct",
    "last_updated",
]

_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


def trackable(ticker: str | None) -> bool:
    """Only real stock/ETF tickers - skip CUSIPs (Treasuries) and blanks."""
    return bool(ticker and _TICKER_RE.match(ticker))


def _to_date(s: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(s, "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return None


class Prices:
    """yfinance price lookups with in-run caching."""

    def __init__(self) -> None:
        self._on: dict[tuple[str, str], float | None] = {}
        self._cur: dict[str, float | None] = {}

    def on(self, ticker: str, date_str: str) -> float | None:
        d = _to_date(date_str)
        if not HAS_YF or not d:
            return None
        key = (ticker, date_str)
        if key in self._on:
            return self._on[key]
        val = None
        try:
            # closing price on the first trading day on/after the date
            hist = yf.Ticker(ticker).history(
                start=d.isoformat(),
                end=(d + dt.timedelta(days=7)).isoformat(),
                auto_adjust=True)
            if not hist.empty:
                val = round(float(hist["Close"].iloc[0]), 2)
        except Exception:  # noqa: BLE001
            val = None
        self._on[key] = val
        return val

    def current(self, ticker: str) -> float | None:
        if not HAS_YF:
            return None
        if ticker in self._cur:
            return self._cur[ticker]
        val = None
        try:
            hist = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
            if not hist.empty:
                val = round(float(hist["Close"].iloc[-1]), 2)
        except Exception:  # noqa: BLE001
            val = None
        self._cur[ticker] = val
        return val


def _pct(frm, to) -> str:
    try:
        frm, to = float(frm), float(to)
        if frm:
            return f"{round((to - frm) / frm * 100, 1)}"
    except (ValueError, TypeError):
        pass
    return ""


def load_portfolio(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {row["id"]: row for row in csv.DictReader(f)}


def save_portfolio(path: Path, rows: dict[str, dict]) -> None:
    ordered = sorted(rows.values(),
                     key=lambda r: (r["status"] != "open", r["disclosure_date"]))
    with path.open("w", newline="", encoding="utf-8") as f:
        wtr = csv.DictWriter(f, fieldnames=FIELDNAMES)
        wtr.writeheader()
        for r in ordered:
            wtr.writerow({k: r.get(k, "") for k in FIELDNAMES})


def _trade_id(t: dict) -> str:
    return f"{t['ticker']}|{t['trade_date']}|{t['pdf_url']}"


def update(path: Path, trades: list[dict], errors: list[str]) -> dict:
    """Update portfolio.csv from this run's trades. Returns summary counts."""
    if not HAS_YF:
        errors.append("yfinance unavailable - performance tracking skipped.")
        return {"opened": 0, "closed": 0, "updated": 0, "skipped": 0}

    rows = load_portfolio(path)
    px = Prices()
    today = dt.date.today().isoformat()
    opened = closed = skipped = 0

    buys = [t for t in trades if t["txn"] == "Buy" and trackable(t["ticker"])]
    sells = [t for t in trades if t["txn"] == "Sell" and trackable(t["ticker"])]
    skipped += sum(1 for t in trades
                   if t["txn"] in ("Buy", "Sell") and not trackable(t["ticker"]))

    # 1) open new buy positions
    for t in buys:
        tid = _trade_id(t)
        if tid in rows:
            continue
        entry = px.on(t["ticker"], t["trade_date"])
        disc = px.on(t["ticker"], t["notification_date"])
        rows[tid] = {
            "id": tid, "filer": t["filer"], "chamber": t["chamber"],
            "ticker": t["ticker"],
            "company": t.get("company") or t["asset"][:24],
            "trade_date": t["trade_date"], "disclosure_date": t["notification_date"],
            "amount": t["amount"],
            "entry_price": entry if entry is not None else "",
            "disclosure_price": disc if disc is not None else "",
            "status": "open",
            "sell_trade_date": "", "sell_disclosure_date": "", "sell_price": "",
            "current_price": "", "current_date": "",
            "ret_since_disclosure_pct": "", "ret_since_entry_pct": "",
            "realized_disclosure_pct": "", "realized_entry_pct": "",
            "last_updated": today,
        }
        opened += 1

    # 2) close open positions when the same member sells the ticker
    for t in sells:
        open_matches = [r for r in rows.values()
                        if r["status"] == "open"
                        and r["filer"] == t["filer"]
                        and r["ticker"] == t["ticker"]]
        if not open_matches:
            continue  # a sell of something we never tracked buying - ignore
        # close the oldest open position
        r = sorted(open_matches, key=lambda x: x["disclosure_date"])[0]
        sell_px = px.on(t["ticker"], t["notification_date"])
        r["status"] = "closed"
        r["sell_trade_date"] = t["trade_date"]
        r["sell_disclosure_date"] = t["notification_date"]
        r["sell_price"] = sell_px if sell_px is not None else ""
        r["realized_disclosure_pct"] = _pct(r["disclosure_price"], sell_px)
        r["realized_entry_pct"] = _pct(r["entry_price"], sell_px)
        r["last_updated"] = today
        closed += 1

    # 3) refresh current price + returns for all still-open positions
    updated = 0
    for r in rows.values():
        if r["status"] != "open":
            continue
        cur = px.current(r["ticker"])
        if cur is not None:
            r["current_price"] = cur
            r["current_date"] = today
            r["ret_since_disclosure_pct"] = _pct(r["disclosure_price"], cur)
            r["ret_since_entry_pct"] = _pct(r["entry_price"], cur)
        r["last_updated"] = today
        updated += 1

    save_portfolio(path, rows)
    return {"opened": opened, "closed": closed, "updated": updated,
            "skipped": skipped, "total": len(rows)}
