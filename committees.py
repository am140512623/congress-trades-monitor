"""
Committee tagging for congressional trades.

Uses the well-maintained `unitedstates/congress-legislators` dataset to map each
filer to their full-committee memberships, then applies a heuristic to flag when
a member trades a stock in a sector their committee oversees (e.g. an Energy &
Commerce member trading a pharma stock).

Data is fuzzy by nature (committee jurisdiction vs. a company's sector), so the
sector match is explicitly a heuristic, not a legal determination.
"""

from __future__ import annotations

import json
import urllib.request

_BASE = "https://unitedstates.github.io/congress-legislators/"
_UA = "CongressTradesMonitor/1.0"

# Curated map: full-committee name substring -> sectors it broadly oversees.
COMMITTEE_SECTORS: dict[str, set[str]] = {
    "Financial Services": {"financials"},
    "Banking": {"financials"},
    "Ways and Means": {"financials", "healthcare"},
    "Energy and Commerce": {"energy", "healthcare", "technology", "telecom"},
    "Energy and Natural Resources": {"energy", "utilities"},
    "Natural Resources": {"energy", "utilities", "materials"},
    "Environment and Public Works": {"energy", "utilities", "materials"},
    "Armed Services": {"defense", "industrials"},
    "Homeland Security": {"defense", "technology"},
    "Agriculture": {"agriculture", "consumer staples"},
    "Health": {"healthcare"},
    "Science, Space": {"technology"},
    "Commerce, Science": {"technology", "telecom", "industrials"},
    "Transportation": {"industrials", "airlines"},
    "Veterans": {"healthcare"},
    "Intelligence": {"defense", "technology"},
    "Foreign": {"defense"},
}

# Best-effort ticker -> sector for commonly traded names. Unknown tickers are
# simply not matched (and noted), rather than guessed.
TICKER_SECTORS: dict[str, str] = {
    # technology
    "AAPL": "technology", "MSFT": "technology", "NVDA": "technology",
    "GOOGL": "technology", "GOOG": "technology", "META": "technology",
    "AMZN": "technology", "TSM": "technology", "AVGO": "technology",
    "INTC": "technology", "AMD": "technology", "CRM": "technology",
    "ORCL": "technology", "ADBE": "technology", "CSCO": "technology",
    "IBM": "technology", "QCOM": "technology", "TXN": "technology",
    "PLTR": "technology", "NOW": "technology", "MU": "technology",
    # telecom
    "T": "telecom", "VZ": "telecom", "TMUS": "telecom", "CMCSA": "telecom",
    # financials
    "JPM": "financials", "BAC": "financials", "WFC": "financials",
    "GS": "financials", "MS": "financials", "C": "financials",
    "AXP": "financials", "V": "financials", "MA": "financials",
    "BLK": "financials", "SCHW": "financials", "PYPL": "financials",
    # healthcare / pharma
    "JNJ": "healthcare", "PFE": "healthcare", "MRK": "healthcare",
    "ABBV": "healthcare", "LLY": "healthcare", "UNH": "healthcare",
    "TMO": "healthcare", "ABT": "healthcare", "BMY": "healthcare",
    "AMGN": "healthcare", "GILD": "healthcare", "CVS": "healthcare",
    "BDX": "healthcare", "MDT": "healthcare",
    # energy / utilities
    "XOM": "energy", "CVX": "energy", "COP": "energy", "SLB": "energy",
    "EOG": "energy", "OXY": "energy", "PSX": "energy", "MPC": "energy",
    "EQT": "energy", "VST": "utilities", "NEE": "utilities", "DUK": "utilities",
    "SO": "utilities", "GEV": "utilities",
    # defense / industrials
    "LMT": "defense", "RTX": "defense", "NOC": "defense", "GD": "defense",
    "BA": "defense", "GE": "industrials", "HON": "industrials",
    "CAT": "industrials", "DE": "industrials", "MMM": "industrials",
    # airlines / transport
    "DAL": "airlines", "UAL": "airlines", "AAL": "airlines", "LUV": "airlines",
    # consumer staples / agriculture
    "PG": "consumer staples", "KO": "consumer staples", "PEP": "consumer staples",
    "WMT": "consumer staples", "COST": "consumer staples", "ADM": "agriculture",
    "MOS": "agriculture", "CTVA": "agriculture", "DE ": "agriculture",
    # materials
    "ALB": "materials", "FCX": "materials", "LIN": "materials", "NUE": "materials",
}


def _get(name: str):
    req = urllib.request.Request(_BASE + name, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


class CommitteeIndex:
    """Loads legislator + committee data and answers committee/sector questions."""

    def __init__(self) -> None:
        legislators = _get("legislators-current.json")
        committees = _get("committees-current.json")
        membership = _get("committee-membership-current.json")

        # thomas_id -> committee display name (full committees only)
        self._committee_name = {c["thomas_id"]: c["name"] for c in committees}
        full_ids = set(self._committee_name)

        # bioguide -> set of full-committee names
        self._bio_committees: dict[str, set[str]] = {}
        for thomas_id, members in membership.items():
            if thomas_id not in full_ids:
                continue  # skip subcommittees
            cname = self._committee_name[thomas_id]
            for m in members:
                bio = m.get("bioguide")
                if bio:
                    self._bio_committees.setdefault(bio, set()).add(cname)

        # name lookup: last_lower -> list of (first_lower, state, chamber, bioguide)
        self._by_last: dict[str, list[tuple]] = {}
        for leg in legislators:
            term = leg["terms"][-1]
            chamber = "House" if term["type"] == "rep" else "Senate"
            rec = (leg["name"]["first"].lower(), term.get("state", ""),
                   chamber, leg["id"]["bioguide"])
            self._by_last.setdefault(leg["name"]["last"].lower(), []).append(rec)

    def bioguide_for(self, first: str, last: str, state: str, chamber: str) -> str | None:
        cands = self._by_last.get((last or "").strip().lower(), [])
        if not cands:
            return None
        fl = (first or "").strip().lower()
        st = (state or "").strip().upper()[:2]
        # 1) exact-ish: chamber + state + first-name prefix
        for cf, cstate, cch, bio in cands:
            if cch == chamber and (not st or cstate == st):
                if not fl or fl.startswith(cf[:3]) or cf.startswith(fl[:3]):
                    return bio
        # 2) fallback: chamber + state only
        for cf, cstate, cch, bio in cands:
            if cch == chamber and st and cstate == st:
                return bio
        # 3) last resort: unique last name
        if len(cands) == 1:
            return cands[0][3]
        return None

    def committees_for(self, first: str, last: str, state: str, chamber: str) -> list[str]:
        bio = self.bioguide_for(first, last, state, chamber)
        if not bio:
            return []
        return sorted(self._bio_committees.get(bio, set()))

    @staticmethod
    def sector_for(ticker: str | None) -> str | None:
        if not ticker:
            return None
        return TICKER_SECTORS.get(ticker.strip().upper())

    def committee_sector_matches(self, committees: list[str], ticker: str | None) -> list[tuple[str, str]]:
        """Return (committee, sector) pairs where a committee oversees the ticker's sector."""
        sector = self.sector_for(ticker)
        if not sector:
            return []
        hits = []
        for c in committees:
            for needle, sectors in COMMITTEE_SECTORS.items():
                if needle in c and sector in sectors:
                    hits.append((c, sector))
                    break
        return hits
