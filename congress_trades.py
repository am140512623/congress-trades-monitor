#!/usr/bin/env python3
"""
Weekly U.S. Congress stock-trade disclosure monitor.

Pulls Periodic Transaction Report (PTR) filings from the official
House Clerk financial-disclosure dataset, filters to the most recent
completed week, flags priority-trader activity, builds a Markdown
report in the project's standard format, and delivers it to Telegram.

Data source (no JS / no API key required):
    https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip
    -> contains {YEAR}FD.xml : an index of every disclosure filing that year.

Senate PTR detail (efdsearch.senate.gov) requires a CSRF-token session and
is logged as a data gap rather than scraped, mirroring the report template.

Usage:
    python congress_trades.py                 # last completed week, send to Telegram
    python congress_trades.py --no-send       # build report only, print to stdout
    python congress_trades.py --week 2026-06-15   # force a specific Monday start
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

try:
    import pdfplumber  # for reading per-trade detail out of House PTR PDFs
    HAS_PDF = True
except ImportError:
    HAS_PDF = False

try:
    from curl_cffi import requests as cffi_requests  # bypasses Akamai on Senate site
    HAS_CURL = True
except ImportError:
    HAS_CURL = False

try:
    from committees import CommitteeIndex, first_names_match  # tagging + name matching
    HAS_COMMITTEES = True
except ImportError:
    HAS_COMMITTEES = False

    def first_names_match(a: str, b: str) -> bool:  # noqa: D103 - fallback, no nicknames
        a, b = (a or "").strip().lower(), (b or "").strip().lower()
        if not a or not b:
            return True
        a, b = a.split()[0], b.split()[0]
        return a.startswith(b[:3]) or b.startswith(a[:3])

try:
    import tracker  # performance tracking of buy positions -> portfolio.csv
    HAS_TRACKER = True
except ImportError:
    HAS_TRACKER = False

PORTFOLIO_FILE = Path(__file__).resolve().parent / "portfolio.csv"

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

PROJECT_DIR = Path(__file__).resolve().parent
REPORTS_DIR = PROJECT_DIR / "reports"
CONFIG_FILE = PROJECT_DIR / "telegram_config.json"

HOUSE_FD_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PTR_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"

# A trade disclosed this many days after execution carries no actionable signal -
# the price has long since moved. Those trades are summarised as a single count in
# the Telegram feed instead of one line each; the markdown report still lists them
# in full (Section 4) because the STOCK Act angle still matters.
STALE_DISCLOSURE_DAYS = 180
# STOCK Act reporting deadline; anything past this is "late" but may still be useful.
STOCK_ACT_DEADLINE_DAYS = 45

# (first name, last name) - last name may be compound (e.g. "Wasserman Schultz").
PRIORITY_TRADERS = [
    ("Nancy", "Pelosi"), ("Dan", "Crenshaw"), ("Josh", "Gottheimer"),
    ("Ro", "Khanna"), ("Tommy", "Tuberville"), ("Mark", "Green"),
    ("Debbie", "Wasserman Schultz"), ("Sheldon", "Whitehouse"),
    ("David", "McCormick"), ("Kevin", "Hern"),
]
PRIORITY_DISPLAY = [f"{f} {l}" for f, l in PRIORITY_TRADERS]

def is_priority(first: str, last: str) -> bool:
    """Match on last name + a loose first-name check, to avoid both
    false negatives (compound surnames) and false positives (common surnames
    like 'Green' shared by non-priority members).

    Senate eFD files Tuberville under "Thomas H" while this list says "Tommy";
    every one of his trades was silently unflagged until first_names_match()
    started bridging that.
    """
    ll = (last or "").strip().lower()
    for pf, pl in PRIORITY_TRADERS:
        if ll == pl.lower() and first_names_match(first, pf):
            return True
    return False

ALLOWED_SOURCES = [
    "disclosures-clerk.house.gov (primary - House PTRs)",
    "efts.senate.gov / efdsearch.senate.gov (primary - Senate PTRs)",
    "quiverquant.com (verification)",
    "opensecrets.org (verification)",
    "official house.gov / senate.gov committee pages",
]

USER_AGENT = "CongressTradesMonitor/1.0 (personal weekly report)"


# --------------------------------------------------------------------------- #
# Telegram credentials
# --------------------------------------------------------------------------- #

def load_telegram_creds() -> tuple[str | None, str | None]:
    """Prefer environment variables (GitHub Actions secrets); fall back to local JSON."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        return token, chat
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data.get("bot_token"), str(data.get("chat_id"))
    return None, None


# --------------------------------------------------------------------------- #
# Date window
# --------------------------------------------------------------------------- #

def last_completed_week(today: dt.date) -> tuple[dt.date, dt.date]:
    """Return (Monday, Sunday) of the week immediately before `today`'s week."""
    this_monday = today - dt.timedelta(days=today.weekday())
    start = this_monday - dt.timedelta(days=7)
    end = start + dt.timedelta(days=6)
    return start, end


def with_retries(fn, *, attempts: int = 3, base_delay: float = 2.0, what: str = "request"):
    """Call fn(), retrying on any exception with exponential backoff."""
    last = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < attempts:
                wait = base_delay * (2 ** (i - 1))
                print(f"  {what} failed (attempt {i}/{attempts}): {e} - retrying in {wait:.0f}s",
                      file=sys.stderr)
                time.sleep(wait)
    raise last


# --------------------------------------------------------------------------- #
# House data
# --------------------------------------------------------------------------- #

def fetch_house_index(year: int) -> list[dict]:
    """Download and parse the House FD index for `year`. Returns list of filing dicts."""
    url = HOUSE_FD_ZIP.format(year=year)

    def _get():
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()

    raw = with_retries(_get, what=f"House index {year}")
    zf = zipfile.ZipFile(io.BytesIO(raw))
    xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
    root = ET.fromstring(zf.read(xml_name))

    filings = []
    for m in root.findall("Member"):
        def g(tag: str) -> str:
            el = m.find(tag)
            return (el.text or "").strip() if el is not None else ""
        filings.append({
            "prefix": g("Prefix"),
            "last": g("Last"),
            "first": g("First"),
            "suffix": g("Suffix"),
            "type": g("FilingType"),     # 'P' = Periodic Transaction Report
            "state_dst": g("StateDst"),
            "year": g("Year"),
            "filing_date": g("FilingDate"),  # MM/DD/YYYY
            "doc_id": g("DocID"),
        })
    return filings


def parse_mmddyyyy(s: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(s, "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return None


def ptrs_in_window(filings: list[dict], start: dt.date, end: dt.date) -> list[dict]:
    out = []
    for f in filings:
        if f["type"] != "P":
            continue
        fd = parse_mmddyyyy(f["filing_date"])
        if fd and start <= fd <= end:
            f = dict(f)
            f["filing_date_obj"] = fd
            f["full_name"] = f"{f['first']} {f['last']}".strip()
            f["is_priority"] = is_priority(f["first"], f["last"])
            year = fd.year
            f["pdf_url"] = HOUSE_PTR_PDF.format(year=year, doc=f["doc_id"])
            out.append(f)
    out.sort(key=lambda r: (not r["is_priority"], r["last"]))
    return out


# --------------------------------------------------------------------------- #
# PTR PDF parsing  (extract per-trade detail from each official filing PDF)
# --------------------------------------------------------------------------- #

TXN_TYPE = {"P": "Buy", "S": "Sell", "E": "Exchange"}

_ANCHOR = re.compile(
    r"\b([PSE])\s*(?:\((?:partial|full)\))?\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})(.*)"
)
_TICK_ADJ = re.compile(r"\(([A-Z0-9]{1,9})\)\s*\[([A-Z]{2})\]")
_TICK_ANY = re.compile(r"\(([A-Z]{1,5})\)")
_ASSET_TAG = re.compile(r"\[([A-Z]{2})\]")
_MONEY = re.compile(r"\$[\d,]+")


def amount_high(amount: str) -> int:
    """Return the upper dollar bound of an amount range like '$15,001 - $50,000'."""
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", amount)]
    return max(nums) if nums else 0


def download_pdf(url: str, dest: Path) -> Path:
    def _get():
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()

    dest.write_bytes(with_retries(_get, what=f"PDF {dest.name}"))
    return dest


def parse_ptr_pdf(path: Path) -> list[dict]:
    """Extract a list of trade dicts from a House PTR PDF's text layer."""
    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    lines = text.splitlines()
    anchors = [i for i, ln in enumerate(lines) if _ANCHOR.search(ln)]
    rows = []
    for idx, i in enumerate(anchors):
        m = _ANCHOR.search(lines[i])
        ttype, tdate, ndate, _rest = m.groups()
        end = anchors[idx + 1] if idx + 1 < len(anchors) else min(len(lines), i + 7)
        window = " ".join(lines[i:end])

        amts = _MONEY.findall(window)
        amount = " - ".join(amts[:2]) if amts else "?"

        tm = _TICK_ADJ.search(window)
        if tm:
            ticker, ctype = tm.groups()
        else:
            pm = _TICK_ANY.search(window)
            cm = _ASSET_TAG.search(window)
            ticker = pm.group(1) if pm else None
            ctype = cm.group(1) if cm else None

        name = window
        tagm = _ASSET_TAG.search(window)
        if tagm:
            name = window[:tagm.start()]
        name = _ANCHOR.split(name)[0]
        name = re.sub(r"^(SP|JT|DC)\s+", "", name.strip())
        name = re.sub(r"\s+", " ", name).strip(" -")

        rows.append({
            "asset": name[:55],
            "ticker": ticker,
            "asset_type": ctype,
            "txn": TXN_TYPE.get(ttype, ttype),
            "trade_date": tdate,
            "notification_date": ndate,
            "amount": amount,
            "amount_high": amount_high(amount),
        })
    return rows


def enrich_with_trades(ptrs: list[dict], errors: list[str]) -> list[dict]:
    """Download each PTR PDF and attach parsed trades. Returns flat list of trades."""
    cache = PROJECT_DIR / "_pdfs"
    cache.mkdir(exist_ok=True)
    all_trades = []
    for p in ptrs:
        p["trades"] = []
        if not HAS_PDF:
            continue
        dest = cache / f"{p['doc_id']}.pdf"
        try:
            if not dest.exists():
                download_pdf(p["pdf_url"], dest)
            trades = parse_ptr_pdf(dest)
            # the official filing/disclosure date is the index FilingDate (the date
            # we actually filtered the window on) - normalise to MM/DD/YYYY.
            fd = p.get("filing_date_obj")
            disclosure = f"{fd:%m/%d/%Y}" if fd else p["filing_date"]
            for t in trades:
                t["filer"] = p["full_name"]
                t["first"] = p["first"]
                t["last"] = p["last"]
                t["chamber"] = "House"
                t["state_dst"] = p["state_dst"]
                t["is_priority"] = p["is_priority"]
                t["pdf_url"] = p["pdf_url"]
                # the House index has no amendment flag (FilingType is 'P' for
                # every PTR, original or corrected), so this stays False here.
                t["is_amendment"] = False
                t["pdf_notification_date"] = t["notification_date"]  # keep for reference
                t["notification_date"] = disclosure                  # in-window filing date
            p["trades"] = trades
            all_trades.extend(trades)
        except Exception as e:  # noqa: BLE001
            errors.append(f"PDF parse failed for {p['full_name']} (#{p['doc_id']}): {e}")
    return all_trades


def delay_days(t: dict) -> int | None:
    td = parse_mmddyyyy(t["trade_date"])
    nd = parse_mmddyyyy(t["notification_date"])
    return (nd - td).days if td and nd else None


def tag_committees(trades: list[dict], errors: list[str]):
    """Attach committee memberships + sector-match info to each trade (in place).

    Returns the loaded CommitteeIndex (or None), so the caller can reuse its
    legislator roster without fetching it twice.
    """
    for t in trades:
        t.setdefault("committees", [])
        t.setdefault("committee_match", [])
    if not HAS_COMMITTEES:
        errors.append("committees.py unavailable - committee tagging skipped.")
        return None
    try:
        idx = with_retries(CommitteeIndex, what="committee data", attempts=2)
    except Exception as e:  # noqa: BLE001
        errors.append(f"Committee data fetch failed - tagging skipped: {e}")
        return None
    # cache per (filer, chamber) to avoid repeat lookups
    cache: dict[tuple, list[str]] = {}
    for t in trades:
        first = t.get("first", "")
        last = t.get("last", "")
        state = t["state_dst"][:2] if t["chamber"] == "House" else ""
        key = (t["filer"], t["chamber"])
        if key not in cache:
            cache[key] = idx.committees_for(first, last, state, t["chamber"])
        t["committees"] = cache[key]
        t["committee_match"] = idx.committee_sector_matches(t["committees"], t["ticker"])
    return idx


# --------------------------------------------------------------------------- #
# Senate data  (efdsearch.senate.gov - behind Akamai, needs curl_cffi)
# --------------------------------------------------------------------------- #

SENATE_BASE = "https://efdsearch.senate.gov"
SENATE_LANDING = SENATE_BASE + "/search/"
SENATE_HOME = SENATE_BASE + "/search/home/"
SENATE_DATA = SENATE_BASE + "/search/report/data/"


def _csrf(text: str) -> str | None:
    m = re.search(r"name=['\"]csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", text)
    return m.group(1) if m else None


class AkamaiBlocked(Exception):
    """Raised when the Senate site denies access (Akamai bot protection)."""


def _senate_session():
    s = cffi_requests.Session(impersonate="chrome")

    def _land():
        r = s.get(SENATE_LANDING, timeout=30)
        if r.status_code == 403 or "Access Denied" in r.text[:500]:
            raise AkamaiBlocked(f"landing returned {r.status_code} (Akamai)")
        return r

    r = with_retries(_land, what="Senate landing")
    token = _csrf(r.text) or s.cookies.get("csrftoken")
    s.post(SENATE_HOME,
           data={"prohibition_agreement": "1", "csrfmiddlewaretoken": token},
           headers={"Referer": SENATE_LANDING}, timeout=30)
    token = s.cookies.get("csrftoken") or token
    return s, token


def _senate_listing(s, token, start: dt.date, end: dt.date) -> list[dict]:
    payload = {
        "draw": "1", "start": "0", "length": "100",
        "report_types": "[11]",          # 11 = Periodic Transaction Report
        # 1 = Senator. An empty list means *every* filer type, which also returns
        # candidates, former senators and reporting individuals (senior staff) -
        # none of whom belong in a report about sitting members' trades.
        "filer_types": "[1]",
        "submitted_start_date": f"{start:%m/%d/%Y} 00:00:00",
        "submitted_end_date": f"{end:%m/%d/%Y} 23:59:59",
        "candidate_state": "", "senator_state": "", "office_id": "",
        "first_name": "", "last_name": "",
    }
    r = s.post(SENATE_DATA, data=payload, headers={
        "Referer": SENATE_LANDING, "X-CSRFToken": token,
        "X-Requested-With": "XMLHttpRequest",
    }, timeout=30)
    out = []
    for row in r.json().get("data", []):
        first, last, office, link_html, date = (list(row) + [""] * 5)[:5]
        href = re.search(r'href="([^"]+)"', link_html)
        # the anchor text carries the report title, which is how eFD marks an
        # amendment. Without it a re-filing of old trades is indistinguishable
        # from an original filed years late.
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", link_html)).strip()
        out.append({"first": first.strip(), "last": last.strip(),
                    "office": office, "href": href.group(1) if href else None,
                    "date": date.strip(), "title": title,
                    "is_amendment": "amend" in title.lower()})
    return out


def _senate_txn(t: str) -> str:
    if t.startswith("Purchase"):
        return "Buy"
    if t.startswith("Sale"):
        return "Sell"
    if t.startswith("Exchange"):
        return "Exchange"
    return t


def parse_senate_ptr(s, url: str) -> list[dict]:
    """Parse the transaction table from an electronic Senate PTR view page."""
    r = s.get(url, timeout=30)
    trades = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in cells]
        if len(cells) < 8 or not re.match(r"^\d+$", cells[0]):
            continue
        _num, tdate, _owner, ticker, asset, _atype, ttype, amount = cells[:8]
        ticker = ticker if ticker and ticker != "--" else None
        trades.append({
            "asset": asset[:55],
            "ticker": ticker,
            "txn": _senate_txn(ttype),
            "trade_date": tdate,
            "amount": amount,
            "amount_high": amount_high(amount),
        })
    return trades


def fetch_senate_trades(start: dt.date, end: dt.date, errors: list[str]) -> list[dict]:
    if not HAS_CURL:
        errors.append("curl_cffi not installed - Senate trades NOT fetched. "
                      "Install requirements.txt.")
        return []
    try:
        s, token = _senate_session()
        listing = with_retries(lambda: _senate_listing(s, token, start, end),
                               what="Senate listing")
    except AkamaiBlocked as e:
        errors.append(f"Senate eFD blocked by Akamai bot protection ({e}). "
                      "This can happen from datacenter IPs (e.g. GitHub Actions). "
                      "House data is unaffected.")
        return []
    except Exception as e:  # noqa: BLE001
        errors.append(f"Senate eFD query failed: {e}")
        return []

    all_trades = []
    for f in listing:
        # eFD name fields carry stray punctuation ("Moran," / "Peters."), which
        # breaks roster lookups and splits one filer into two in the portfolio.
        f["first"] = f["first"].strip().strip(",.").strip()
        f["last"] = f["last"].strip().strip(",.").strip()
        full = f"{f['first']} {f['last']}".strip()
        if not f["href"] or "/ptr/" not in f["href"]:
            errors.append(f"Senate paper/non-electronic filing skipped (not parseable): {full}")
            continue
        url = SENATE_BASE + f["href"]
        try:
            ts = parse_senate_ptr(s, url)
        except Exception as e:  # noqa: BLE001
            errors.append(f"Senate PTR parse failed ({full}): {e}")
            continue
        for t in ts:
            t["filer"] = full
            t["first"] = f["first"]
            t["last"] = f["last"]
            t["chamber"] = "Senate"
            t["state_dst"] = f["office"].split("(")[0].strip()[:14]
            t["is_priority"] = is_priority(f["first"], f["last"])
            t["notification_date"] = f["date"]  # Senate reports filing date, not per-row
            t["is_amendment"] = f["is_amendment"]
            t["pdf_url"] = url
            all_trades.append(t)
    return all_trades


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #

def _verify(t: dict) -> str:
    """Markdown verify link to the official PTR PDF for a trade."""
    return f"[verify]({t['pdf_url']})"


_CO_BOILERPLATE = re.compile(
    r"\b(Common Stock|Ordinary Shares?|Class [A-C]|Corporation|Incorporated|"
    r"Inc\.?|plc|Company|Co\.?|Ltd\.?|L\.?P\.?|Holdings?|Group|Trust|Fund|"
    r"ETF Shares?)\b.*$", re.I)


def short_company(asset: str, ticker: str | None = None) -> str:
    """Turn a verbose asset name into a short, readable company name.
    e.g. 'NVIDIA Corporation - Common Stock' -> 'NVIDIA'."""
    s = re.sub(r"\s*\([A-Za-z0-9.\- ]+\)\s*", " ", asset or "")  # drop parentheticals
    s = s.split(" - ")[0]
    s = _CO_BOILERPLATE.sub("", s).strip(" ,-")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:24] or (ticker or "")


def build_report(start: dt.date, end: dt.date, ptrs: list[dict],
                 trades: list[dict], errors: list[str],
                 senate_enabled: bool = False) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    fmt = "%A, %B %d, %Y"
    priority_hits = [p for p in ptrs if p["is_priority"]]
    priority_trades = [t for t in trades if t["is_priority"]]
    high_value = [t for t in trades if t["amount_high"] >= 250_000]
    house_trades = [t for t in trades if t["chamber"] == "House"]
    senate_trades = [t for t in trades if t["chamber"] == "Senate"]

    lines: list[str] = []
    w = lines.append

    w("# U.S. Congress Stock Trade Disclosure - Weekly Monitor\n")
    w(f"**Week Covered:** {start.strftime(fmt)} - {end.strftime(fmt)}  ")
    w(f"**Generated On:** {now.strftime('%Y-%m-%dT%H:%M:%SZ')} (automated weekly run)\n")
    w("---\n")

    # Data access notice
    w("## Data Access Notice\n")
    w("House PTR detail is parsed from each filing's official PTR PDF "
      "(disclosures-clerk.house.gov). Senate PTR detail is parsed from the "
      "electronic filing pages on efdsearch.senate.gov. Every trade row links back "
      "to its official source (PDF or filing page) for verification.\n")
    w(f"**Verification result:** {len(trades)} individual trade(s) "
      f"({len(house_trades)} House, {len(senate_trades)} Senate) with a filing date "
      f"inside {start:%m/%d/%Y}-{end:%m/%d/%Y} were retrieved and parsed from the "
      "official House Clerk and Senate eFD sources this cycle.\n")
    w("---\n")

    # Section 0 - priority
    w("## Section 0. Priority Trader Activity\n")
    w("**Monitored politicians:** " + ", ".join(PRIORITY_DISPLAY) + "\n")
    if senate_enabled:
        w("> Coverage: both **House** (disclosures-clerk.house.gov) and **Senate** "
          "(efdsearch.senate.gov) electronic PTR filings are included. Senate "
          "**paper** filings are scanned images and cannot be parsed - those are "
          "logged in Section 5 if any appear.\n")
    else:
        w("> ⚠️ **Coverage note:** Senate scraping was disabled this run; House only.\n")
    if not priority_trades:
        w("> **No trades disclosed by priority traders this week.**\n")
    else:
        w("| Name | Ticker | Buy/Sell | Amount | Trade Date | Disclosure Date | Verify |")
        w("|------|--------|----------|--------|------------|-----------------|--------|")
        for t in priority_trades:
            w(f"| {t['filer']} | {t['ticker'] or t['asset']} | {t['txn']} | {t['amount']} "
              f"| {t['trade_date']} | {t['notification_date']} | {_verify(t)} |")
        w("")
    w("---\n")

    # Section 1 - all trades (ticker-first, priority filers in bold)
    w("## Section 1. Newly Disclosed Trades (filing date in window)\n")
    w(f"*This report covers trades **disclosed** {start:%b %d} - {end:%b %d, %Y}. "
      "The **Trade Date** is when the trade was executed (often weeks earlier - "
      "members get up to 45 days to report); the **Disclosure Date** is when it was "
      "filed with Congress and is always within this week's window.*\n")
    w("*Priority traders shown in **bold** with a ⭐. Each row links to its official source.*\n")
    if not trades:
        w("*No individual trades were parsed from PTR filings inside the window this cycle.*\n")
    else:
        w("| Flags | Ticker | Company | Buy/Sell | Trader | Amount | Trade Date | Disclosure Date | Verify |")
        w("|-------|--------|---------|----------|--------|--------|------------|-----------------|--------|")
        ordered = sorted(trades, key=lambda t: (not t["is_priority"], t["filer"], t["ticker"] or "zzz"))
        for t in ordered:
            name = f"**{t['filer']}**" if t["is_priority"] else t["filer"]
            flags = ("⭐" if t["is_priority"] else "") + ("🚩" if t.get("committee_match") else "")
            company = short_company(t["asset"], t["ticker"])
            w(f"| {flags or '-'} | {t['ticker'] or '-'} | {company} | {t['txn']} | {name} "
              f"| {t['amount']} | {t['trade_date']} | {t['notification_date']} | {_verify(t)} |")
        w("")
    w("---\n")

    # Section 2 - committee relevance (trades where a committee oversees the sector)
    w("## Section 2. Committee Relevance\n")
    w("*Heuristic: a trade is flagged when the trader sits on a committee whose "
      "jurisdiction broadly covers the stock's sector. Not a determination of "
      "wrongdoing - just a signal worth a look.*\n")
    matched = [t for t in trades if t.get("committee_match")]
    if not matched:
        w("*No committee-sector overlaps detected this week (among tickers with a "
          "known sector).*\n")
    else:
        w("| Trader | Ticker | Sector | Relevant Committee | Buy/Sell | Amount | Verify |")
        w("|--------|--------|--------|--------------------|----------|--------|--------|")
        for t in sorted(matched, key=lambda x: x["filer"]):
            com, sec = t["committee_match"][0]
            name = f"**⭐ {t['filer']}**" if t["is_priority"] else t["filer"]
            w(f"| {name} | {t['ticker']} | {sec} | {com} | {t['txn']} | {t['amount']} "
              f"| {_verify(t)} |")
        w("")
    w("---\n")

    # Section 3 - high value
    w("## Section 3. High-Value Trades (>= $250,000)\n")
    if not high_value:
        w("*No trades with an upper bound at or above $250,000 this week.*\n")
    else:
        w("| Filer | Ticker | Buy/Sell | Amount | Trade Date | Verify |")
        w("|-------|--------|----------|--------|------------|--------|")
        for t in high_value:
            w(f"| {t['filer']} | {t['ticker'] or t['asset']} | {t['txn']} | {t['amount']} "
              f"| {t['trade_date']} | {_verify(t)} |")
        w("")
    w("---\n")

    # Section 4 - delayed disclosures
    w("## Section 4. Delayed Disclosures (> 45 days, STOCK Act)\n")
    delayed = [(t, delay_days(t)) for t in trades]
    delayed = [(t, d) for t, d in delayed if d is not None and d > 45]
    if not delayed:
        w("*No trades exceeded the 45-day STOCK Act disclosure window this cycle.*\n")
    else:
        w("*An amended filing restates an earlier disclosure, so its delay measures "
          "time since the trade, not necessarily time spent unreported.*\n")
        w("| Filer | Ticker | Trade Date | Disclosure Date | Delay (days) | Filing | Verify |")
        w("|-------|--------|------------|-----------------|--------------|--------|--------|")
        for t, d in sorted(delayed, key=lambda x: -x[1]):
            kind = "Amendment" if t.get("is_amendment") else "Original"
            w(f"| {t['filer']} | {t['ticker'] or t['asset']} | {t['trade_date']} "
              f"| {t['notification_date']} | {d} | {kind} | {_verify(t)} |")
        w("")
    w("---\n")

    # Section 5 - data gaps
    w("## Section 5. Missing or Incomplete Data Log\n")
    gaps = [
        "Verification sources (quiverquant.com, opensecrets.org) are JS-rendered "
        "and not cross-checked automatically.",
        "Committee-sector matching is heuristic: it covers tickers with a known "
        "sector and broad committee jurisdictions, not exhaustive coverage.",
    ]
    if not senate_enabled:
        gaps.insert(0, "Senate eFD scraping was disabled this run (--no-senate).")
    else:
        gaps.insert(0, "Senate paper (non-electronic) filings are scanned images and "
                       "cannot be parsed; only electronic Senate PTRs are included.")
    gaps.append("House filings cannot be identified as amendments: the House index "
                "reports FilingType 'P' for every PTR, original or corrected. House "
                "rows in Section 4 therefore all show as 'Original'.")
    if not HAS_PDF:
        gaps.insert(0, "pdfplumber is not installed - per-trade detail was NOT parsed "
                       "this run (filing-level only). Install requirements.txt.")
    for e in errors:
        gaps.append(f"Runtime issue: {e}")
    for i, gtxt in enumerate(gaps, 1):
        w(f"{i}. {gtxt}")
    w("\n---\n")

    # Section 6 - flags
    w("## Section 6. Activity Flags\n")

    status = "Triggered" if priority_trades else "Not triggered"
    reason = (f"{len(priority_trades)} trade(s) by priority traders: "
              + ", ".join(sorted({t['filer'] for t in priority_trades})) + "."
              if priority_trades else
              "No trades from the monitored politicians within the window.")
    w(f"### FLAG_PRIORITY_TRADER_ACTIVITY\n**Status:** {status}  \n**Reason:** {reason}\n")

    status = "Triggered" if high_value else "Not triggered"
    reason = (f"{len(high_value)} trade(s) with an upper bound >= $250,000."
              if high_value else "No trades >= $250,000 this week.")
    w(f"### FLAG_LARGE_DISCLOSURE\n**Status:** {status}  \n**Reason:** {reason}\n")

    # sector/ticker clustering: same ticker traded by 2+ different members
    from collections import Counter
    ticker_filers: dict[str, set] = {}
    for t in trades:
        if t["ticker"]:
            ticker_filers.setdefault(t["ticker"], set()).add(t["filer"])
    clustered = {tk: f for tk, f in ticker_filers.items() if len(f) >= 2}
    status = "Triggered" if clustered else "Not triggered"
    reason = ("; ".join(f"{tk} traded by {len(f)} members" for tk, f in clustered.items())
              if clustered else
              "No single ticker was traded by 2+ different members this week.")
    w(f"### FLAG_SECTOR_CLUSTER\n**Status:** {status}  \n**Reason:** {reason}\n")

    # repeated activity: same member 5+ trades
    filer_counts = Counter(t["filer"] for t in trades)
    heavy = {f: c for f, c in filer_counts.items() if c >= 5}
    status = "Triggered" if heavy else "Not triggered"
    reason = ("; ".join(f"{f}: {c} trades" for f, c in heavy.items())
              if heavy else "No member disclosed 5+ trades this week.")
    w(f"### FLAG_HIGH_ACTIVITY_TRADER\n**Status:** {status}  \n**Reason:** {reason}\n")

    delayed_n = sum(1 for t in trades if (d := delay_days(t)) is not None and d > 45)
    status = "Triggered" if delayed_n else "Not triggered"
    reason = (f"{delayed_n} trade(s) disclosed more than 45 days after execution."
              if delayed_n else "All parsed trades disclosed within 45 days.")
    w(f"### FLAG_LATE_DISCLOSURE\n**Status:** {status}  \n**Reason:** {reason}\n")

    # committee-sector match (heuristic)
    cm = [t for t in trades if t.get("committee_match")]
    status = "Triggered" if cm else "Not triggered"
    if cm:
        examples = "; ".join(
            f"{t['filer']} traded {t['ticker']} ({t['committee_match'][0][1]}) - "
            f"sits on {t['committee_match'][0][0]}"
            for t in cm[:5])
        reason = f"{len(cm)} trade(s) overlap a trader's committee jurisdiction. {examples}"
    else:
        reason = ("No committee-sector overlaps among tickers with a known sector "
                  "(committee tagging unavailable counts as none).")
    w(f"### FLAG_COMMITTEE_SECTOR_MATCH\n**Status:** {status}  \n**Reason:** {reason}\n")
    w("---\n")

    # Methodology
    w("## Methodology & Source Log\n")
    w("| Source | Role | Access This Cycle |")
    w("|--------|------|-------------------|")
    w("| disclosures-clerk.house.gov (index) | Primary (House PTRs) | Index parsed (machine-readable) |")
    w("| disclosures-clerk.house.gov (PTR PDFs) | Primary (House detail) | "
      f"{len(house_trades)} trades parsed from {len(ptrs)} filings |")
    w("| efdsearch.senate.gov | Primary (Senate PTRs) | "
      + (f"{len(senate_trades)} trades parsed (electronic filings)"
         if senate_enabled else "Disabled this run") + " |")
    w("| quiverquant.com / opensecrets.org | Verification | Not cross-checked - JS-rendered |")
    w("\n---\n")
    w("*Report generated automatically. No opinions, predictions, or trading "
      "recommendations. Each trade links to its official source PDF for verification.*")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Telegram delivery
# --------------------------------------------------------------------------- #

def tg_send(token: str, chat_id: str, text: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": "true"}
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def esc(s: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def trade_line(t: dict) -> str:
    """One trade, markers FIRST so priority/committee signals are unmissable:

    ⭐🚩 NVDA (NVIDIA) - 🔴 Sell - Nancy Pelosi
        $1,001-$15,000 · traded 06/16 · 🚩 tech committee · verify
    """
    # leading markers - most important signals, before the ticker
    markers = ""
    if t["is_priority"]:
        markers += "⭐"
    if t.get("committee_match"):
        markers += "🚩"
    d = delay_days(t)
    if d is not None and d > STOCK_ACT_DEADLINE_DAYS:
        markers += "⏰"
    markers = (markers + " ") if markers else ""

    ticker = esc(t["ticker"] or "—")
    company = esc(short_company(t["asset"], t["ticker"]))
    name = esc(t["filer"])
    if t["is_priority"]:
        name = f"<b>{name}</b>"
    chamber = "House" if t["chamber"] == "House" else "Senate"
    side = "🟢 Buy" if t["txn"] == "Buy" else ("🔴 Sell" if t["txn"] == "Sell" else esc(t["txn"]))
    amt = esc(t["amount"])
    note = ""
    if t.get("committee_match"):
        note = f"🚩 <i>{esc(t['committee_match'][0][1])} committee</i> · "
    # show when it was filed, not just when it was traded - a bare 2024 trade date
    # under a "this week" header reads like stale data rather than a late filing.
    filed = f"filed {esc(t['notification_date'])}"
    if d is not None and d > STOCK_ACT_DEADLINE_DAYS:
        kind = "amended" if t.get("is_amendment") else "late"
        filed += f" (<b>{d}d {kind}</b>)"
    return (f"{markers}<b>{ticker}</b> ({company}) - {side} - {name} ({chamber})\n"
            f"   {amt} · traded {esc(t['trade_date'])} · {filed} · {note}"
            f"<a href=\"{t['pdf_url']}\">verify</a>")


def make_telegram_messages(start: dt.date, end: dt.date, ptrs: list[dict],
                           trades: list[dict], senate_enabled: bool = False) -> list[str]:
    """Build Telegram-friendly HTML messages (line based), chunked under 4096 chars."""
    # Split off trades disclosed so late they carry no signal. They are counted in
    # the header and summarised at the end rather than given a line each - a single
    # bulk catch-up filing can otherwise be hundreds of dead lines. Age alone
    # decides this: a two-year-old trade is equally unactionable whoever filed it,
    # so priority and committee hits are surfaced in the summary, not exempted.
    def is_stale(t: dict) -> bool:
        d = delay_days(t)
        return d is not None and d > STALE_DISCLOSURE_DAYS

    stale = [t for t in trades if is_stale(t)]
    fresh = [t for t in trades if not is_stale(t)]

    priority_trades = [t for t in trades if t["is_priority"]]
    high_value = [t for t in trades if t["amount_high"] >= 250_000]
    house_n = sum(1 for t in trades if t["chamber"] == "House")
    senate_n = sum(1 for t in trades if t["chamber"] == "Senate")
    fmt = "%b %d, %Y"

    header = [
        "📊 <b>Congress Trades - Weekly Report</b>",
        f"Week: {start.strftime(fmt)} - {end.strftime(fmt)}",
        f"Trades: {len(trades)} ({house_n} House, {senate_n} Senate)  |  "
        f"High-value (≥$250k): {len(high_value)}",
    ]
    if priority_trades:
        names = ", ".join(sorted({t["filer"] for t in priority_trades}))
        header.append(f"⭐ Priority-trader trades: {len(priority_trades)} ({esc(names)})")
    else:
        header.append("⭐ No priority-trader trades this week.")
    if stale:
        header.append(f"🗄 {len(stale)} trade(s) disclosed >{STALE_DISCLOSURE_DAYS} days after "
                      "execution - summarised at the end, full detail in the report.")
    if not senate_enabled:
        header.append("Note: Senate scraping disabled this run.")
    header.append("<i>Legend: ⭐ = priority trader · 🚩 = trades their committee's sector "
                  "· ⏰ = filed past the 45-day deadline</i>")

    # sort: priority first, then by filer, then ticker
    ordered = sorted(fresh, key=lambda t: (not t["is_priority"], t["filer"], t["ticker"] or "zzz"))
    blocks = ["\n".join(header)]
    blocks += [trade_line(t) for t in ordered]

    if stale:
        by_filer: dict[str, list[dict]] = {}
        for t in stale:
            by_filer.setdefault(t["filer"], []).append(t)
        lines = [f"🗄 <b>Stale disclosures</b> (traded >{STALE_DISCLOSURE_DAYS} days "
                 "before filing - no actionable edge, listed for the record)"]
        for filer, ts in sorted(by_filer.items(), key=lambda kv: -len(kv[1])):
            delays = [d for d in (delay_days(t) for t in ts) if d is not None]
            span = f"{min(delays)}-{max(delays)}d late" if delays else "delay unknown"
            amended = sum(1 for t in ts if t.get("is_amendment"))
            tags = []
            if amended:
                tags.append(f"{amended} amended")
            if any(t["is_priority"] for t in ts):
                tags.append("⭐ priority")
            n_match = sum(1 for t in ts if t.get("committee_match"))
            if n_match:
                tags.append(f"🚩 {n_match} committee")
            tag = (", " + ", ".join(tags)) if tags else ""
            lines.append(f"   {esc(filer)}: {len(ts)} trade(s), {span}{tag}")
        blocks.append("\n".join(lines))

    if any(t["chamber"] == "Senate" for t in fresh):
        blocks.append("<i>Senate verify links need efdsearch.senate.gov terms accepted "
                      "once per browser, otherwise they will not open.</i>")

    # chunk blocks into messages under the 4096 limit
    messages, cur = [], ""
    for b in blocks:
        piece = (b + "\n\n")
        if len(cur) + len(piece) > 3800 and cur:
            messages.append(cur.rstrip())
            cur = ""
        cur += piece
    if cur.strip():
        messages.append(cur.rstrip())
    return messages


def deliver(token: str, chat_id: str, messages: list[str]) -> None:
    for i, msg in enumerate(messages, 1):
        r = tg_send(token, chat_id, msg)
        if not r.get("ok"):
            raise RuntimeError(f"Telegram message {i} send failed: {r}")
    print(f"Telegram: {len(messages)} message(s) sent.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run() -> int:
    ap = argparse.ArgumentParser(description="Weekly Congress trade monitor")
    ap.add_argument("--no-send", action="store_true", help="build report only; do not send to Telegram")
    ap.add_argument("--no-pdf", action="store_true", help="skip House PDF parsing (filing-level only, faster)")
    ap.add_argument("--no-senate", action="store_true", help="skip Senate eFD scraping")
    ap.add_argument("--no-track", action="store_true", help="skip portfolio.csv performance tracking")
    ap.add_argument("--week", help="force a Monday start date, YYYY-MM-DD")
    args = ap.parse_args()

    today = dt.date.today()
    if args.week:
        start = dt.datetime.strptime(args.week, "%Y-%m-%d").date()
        end = start + dt.timedelta(days=6)
    else:
        start, end = last_completed_week(today)

    print(f"Reporting window: {start} -> {end}")

    errors: list[str] = []
    filings: list[dict] = []
    # the window can straddle a year boundary; fetch both years' indexes if so
    for year in sorted({start.year, end.year}):
        try:
            idx = fetch_house_index(year)
            print(f"House index {year}: {len(idx)} filings")
            filings.extend(idx)
        except Exception as e:  # noqa: BLE001
            msg = f"House index {year} fetch failed: {e}"
            print(msg, file=sys.stderr)
            errors.append(msg)

    ptrs = ptrs_in_window(filings, start, end)
    print(f"PTR filings in window: {len(ptrs)} "
          f"({sum(p['is_priority'] for p in ptrs)} priority)")

    if args.no_pdf:
        for p in ptrs:
            p["trades"] = []
        trades: list[dict] = []
        print("House PDF parsing skipped (--no-pdf).")
    else:
        if not HAS_PDF:
            errors.append("pdfplumber not installed; House per-trade detail unavailable.")
        trades = enrich_with_trades(ptrs, errors)
        print(f"House trades parsed from PDFs: {len(trades)}")

    senate_enabled = not args.no_senate
    if senate_enabled:
        senate_trades = fetch_senate_trades(start, end, errors)
        print(f"Senate trades parsed: {len(senate_trades)}")
        trades += senate_trades
    else:
        print("Senate scraping skipped (--no-senate).")

    idx = tag_committees(trades, errors)
    n_match = sum(1 for t in trades if t.get("committee_match"))
    print(f"Committee-sector matches: {n_match}")

    # attach a clean company name for tracker/CSV
    for t in trades:
        t["company"] = short_company(t["asset"], t["ticker"])

    if not args.no_track:
        if HAS_TRACKER:
            # without the roster the tracker cannot tell a non-member from a
            # name-match failure, so it leaves existing rows alone.
            summary = tracker.update(PORTFOLIO_FILE, trades, errors,
                                     is_member=idx.is_member if idx else None,
                                     stale_days=STALE_DISCLOSURE_DAYS)
            print(f"Tracker: {summary['opened']} opened, {summary['closed']} closed, "
                  f"{summary['updated']} refreshed, {summary['skipped']} skipped, "
                  f"{summary['stale']} stale (portfolio total: {summary['total']})")
            if summary["dropped_stale"] or summary["reindexed"]:
                print(f"  migration: {summary['dropped_stale']} stale row(s) dropped, "
                      f"{summary['reindexed']} re-indexed")
        else:
            errors.append("tracker.py / yfinance unavailable - tracking skipped.")
    else:
        print("Performance tracking skipped (--no-track).")

    report = build_report(start, end, ptrs, trades, errors, senate_enabled)

    REPORTS_DIR.mkdir(exist_ok=True)
    out_file = REPORTS_DIR / f"congress-trades-{start:%Y-%m-%d}.md"
    out_file.write_text(report, encoding="utf-8")
    print(f"Report saved: {out_file}")

    messages = make_telegram_messages(start, end, ptrs, trades, senate_enabled)

    if args.no_send:
        print("\n" + "=" * 60 + "\n")
        print(report)
        # preview exactly what would have gone to Telegram, so --no-send can be
        # used to check the feed and not just the markdown report.
        print("\n" + "=" * 60)
        print(f"Telegram preview: {len(messages)} message(s) would be sent\n")
        for i, m in enumerate(messages, 1):
            print(f"--- message {i}/{len(messages)} ---")
            print(m + "\n")
        return 0

    token, chat_id = load_telegram_creds()
    if not token or not chat_id:
        print("No Telegram credentials (env vars or telegram_config.json). "
              "Report saved but not sent.", file=sys.stderr)
        return 1

    deliver(token, chat_id, messages)
    return 0


def main() -> int:
    # make console output safe for emoji on Windows code pages
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        return run()
    except Exception as e:  # noqa: BLE001 - last-resort: alert and fail loudly
        import traceback
        traceback.print_exc()
        # try to alert via Telegram so a broken run is never silent
        if "--no-send" not in sys.argv:
            try:
                token, chat_id = load_telegram_creds()
                if token and chat_id:
                    tg_send(token, chat_id,
                            f"⚠️ <b>Congress Trades report FAILED</b>\n{esc(str(e)[:500])}")
            except Exception:  # noqa: BLE001
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
