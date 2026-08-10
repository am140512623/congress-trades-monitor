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
    "priority", "committee_sector", "relevant_committee",
    "trade_date", "disclosure_date", "amount",
    "entry_price", "disclosure_price", "status",
    "sell_trade_date", "sell_disclosure_date", "sell_price",
    "current_price", "current_date",
    "ret_since_disclosure_pct", "ret_since_entry_pct",
    "realized_disclosure_pct", "realized_entry_pct",
    "source_url", "last_updated",
]

# Positions from trades disclosed more than this many days after execution are not
# opened: the price moved long ago, and the member has often already exited via a
# sell that was disclosed before tracking began, so the row would never close.
DEFAULT_STALE_DAYS = 180

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
                     key=lambda r: (r["status"] != "open", _sort_key(r["disclosure_date"])))
    with path.open("w", newline="", encoding="utf-8") as f:
        wtr = csv.DictWriter(f, fieldnames=FIELDNAMES)
        wtr.writeheader()
        for r in ordered:
            wtr.writerow({k: r.get(k, "") for k in FIELDNAMES})


def _sort_key(date_str: str) -> str:
    """MM/DD/YYYY sorts wrong as text ('12/01/2025' > '01/05/2026'), which used to
    pick the wrong position to close across a year boundary. Sort on YYYY-MM-DD."""
    d = _to_date(date_str)
    return d.isoformat() if d else date_str


def _delay_days(trade_date: str, disclosure_date: str) -> int | None:
    td, dd = _to_date(trade_date), _to_date(disclosure_date)
    return (dd - td).days if td and dd else None


def _trade_id(filer: str, ticker: str, trade_date: str, amount: str, occ: int) -> str:
    """Identify a trade by what it *is*, not by which filing disclosed it.

    The old id embedded the filing URL. An amendment restates the same trade
    under a new URL, so it would open a second position for a trade already
    tracked. `occ` distinguishes genuinely repeated trades (same member, ticker,
    day and bracket) within a filing; two such trades split across different
    filings will collapse into one, which is the rarer and safer error.
    """
    return f"{filer}|{ticker}|{trade_date}|{amount}|{occ}"


def _assign_occurrences(items: list, key) -> list[int]:
    """Per-item index within its own key group, preserving input order."""
    seen: dict = {}
    out = []
    for it in items:
        k = key(it)
        seen[k] = seen.get(k, -1) + 1
        out.append(seen[k])
    return out


def migrate(rows: dict[str, dict],
            stale_days: int = DEFAULT_STALE_DAYS) -> tuple[dict[str, dict], dict]:
    """Bring an existing portfolio up to the current rules. Idempotent.

    Two things happen here:
      - ids that embed a filing URL are rebuilt on trade identity, so amendments
        stop opening duplicate positions;
      - positions opened from long-delayed disclosures are dropped, matching what
        `update()` now declines to open in the first place.

    Deliberately does NOT purge rows by roster membership. Filer names arrive
    with artifacts ("Jerry Moran,"), so a failed roster lookup means an unmatched
    name at least as often as a genuine non-member, and the cost of guessing
    wrong is silently deleting a real member's history. Non-members are kept out
    at the source instead, by querying eFD for senators only.
    """
    kept: dict[str, dict] = {}
    dropped_stale = reindexed = 0

    survivors = []
    for r in sorted(rows.values(), key=lambda x: x["id"]):
        d = _delay_days(r.get("trade_date", ""), r.get("disclosure_date", ""))
        # only open rows are dropped: a closed position already produced a
        # realized return, and that record stays valid however late the filing was.
        if r.get("status") == "open" and d is not None and d > stale_days:
            dropped_stale += 1
            continue
        survivors.append(r)

    occs = _assign_occurrences(
        survivors,
        lambda r: (r.get("filer", ""), r.get("ticker", ""),
                   r.get("trade_date", ""), r.get("amount", "")))
    for r, occ in zip(survivors, occs):
        old = r["id"]
        # the pre-migration id was 'TICKER|trade_date|filing_url' - keep the url
        if not r.get("source_url") and old.count("|") == 2:
            r["source_url"] = old.rsplit("|", 1)[-1]
        r["id"] = _trade_id(r.get("filer", ""), r.get("ticker", ""),
                            r.get("trade_date", ""), r.get("amount", ""), occ)
        if r["id"] != old:
            reindexed += 1
        kept[r["id"]] = r

    return kept, {"dropped_stale": dropped_stale, "reindexed": reindexed}


def update(path: Path, trades: list[dict], errors: list[str],
           is_member=None, stale_days: int = DEFAULT_STALE_DAYS) -> dict:
    """Update portfolio.csv from this run's trades. Returns summary counts."""
    if not HAS_YF:
        errors.append("yfinance unavailable - performance tracking skipped.")
        return {"opened": 0, "closed": 0, "updated": 0, "skipped": 0, "stale": 0,
                "total": 0, "dropped_stale": 0, "reindexed": 0}

    rows = load_portfolio(path)
    rows, mig = migrate(rows, stale_days=stale_days)

    # not a purge - just tell the operator if a non-member slipped through the
    # eFD filer-type filter, since that is how 300+ rows once arrived unnoticed.
    if is_member:
        # use the filing's own name fields; splitting the display name would put
        # a middle initial into the surname ('Gary C Peters' -> last 'C Peters').
        unknown = {t["filer"] for t in trades
                   if t["chamber"] == "Senate"
                   and not is_member(t.get("first", ""), t.get("last", ""), "Senate")}
        for name in sorted(unknown):
            errors.append(f"Senate filer not found on the current roster: {name!r} - "
                          "verify they are a sitting senator; their trades are still tracked.")
    px = Prices()
    today = dt.date.today().isoformat()
    opened = closed = skipped = stale = 0

    buys = [t for t in trades if t["txn"] == "Buy" and trackable(t["ticker"])]
    sells = [t for t in trades if t["txn"] == "Sell" and trackable(t["ticker"])]
    skipped += sum(1 for t in trades
                   if t["txn"] in ("Buy", "Sell") and not trackable(t["ticker"]))

    # 1) open new buy positions
    occs = _assign_occurrences(
        buys, lambda t: (t["filer"], t["ticker"], t["trade_date"], t["amount"]))
    for t, occ in zip(buys, occs):
        tid = _trade_id(t["filer"], t["ticker"], t["trade_date"], t["amount"], occ)
        if tid not in rows:
            d = _delay_days(t["trade_date"], t["notification_date"])
            if d is not None and d > stale_days:
                stale += 1
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
                "source_url": t.get("pdf_url", ""),
                "last_updated": today,
            }
            opened += 1
        # always (re)set flag metadata - backfills rows on schema changes too
        r = rows[tid]
        r["priority"] = "yes" if t.get("is_priority") else ""
        if t.get("committee_match"):
            r["committee_sector"] = t["committee_match"][0][1]
            r["relevant_committee"] = t["committee_match"][0][0]
        else:
            r.setdefault("committee_sector", "")
            r.setdefault("relevant_committee", "")

    # 2) close open positions when the same member sells the ticker
    for t in sells:
        open_matches = [r for r in rows.values()
                        if r["status"] == "open"
                        and r["filer"] == t["filer"]
                        and r["ticker"] == t["ticker"]]
        if not open_matches:
            continue  # a sell of something we never tracked buying - ignore
        # close the oldest open position
        r = sorted(open_matches, key=lambda x: _sort_key(x["disclosure_date"]))[0]
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
            "skipped": skipped, "stale": stale, "total": len(rows), **mig}
