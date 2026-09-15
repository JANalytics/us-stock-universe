"""
build_universe.py  —  STEP 1 of the pipeline

Builds the base list of US-listed securities and maps each ticker to a CIK.

Cost: 4 network requests. Total. For the entire market.

Sources (all free, no key, no account)
--------------------------------------
* Nasdaq Trader symbol directories
    nasdaqlisted.txt  — everything listed on Nasdaq
    otherlisted.txt   — NYSE, NYSE American, NYSE Arca, Cboe BZX, IEX
  These also carry two fields worth having: the ETF flag and Nasdaq's
  "Financial Status" code, which flags deficient / delinquent / bankrupt
  issuers. That last one is a genuine red flag you get for free.

* SEC company_tickers_exchange.json — the authoritative ticker -> CIK map.

Output: data/universe.csv
"""

from __future__ import annotations

import csv
import io
import re
import sys

from common import DATA_DIR, make_sec_client, setup_logging

log = setup_logging("universe")

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
SEC_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

EXCHANGE_CODES = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "Z": "Cboe BZX",
    "V": "IEX",
    "Q": "NASDAQ",
}

FINANCIAL_STATUS = {
    "N": "Normal",
    "D": "Deficient",
    "E": "Delinquent",
    "Q": "Bankrupt",
    "G": "Deficient & Bankrupt",
    "H": "Deficient & Delinquent",
    "J": "Delinquent & Bankrupt",
    "K": "Deficient, Delinquent & Bankrupt",
}

# Nasdaq security names are formatted "Issuer Name - Security Class".
# The class suffix is a far more reliable filter than keyword-matching the
# whole string, which is what trips up names like "Trust Bancshares".
_CLASS_RULES: list[tuple[str, str]] = [
    (r"\bwarrant", "Warrant"),
    (r"\bright\b|\brights\b", "Right"),
    (r"\bunit[s]?\b", "Unit"),
    (r"preferred|preference|depositary\s+shar|\bpfd\b", "Preferred"),
    (r"\bnotes?\b|\bdebenture|\bbond\b|\bsubordinated\b", "Debt"),
    (r"\bsubscription\b", "Subscription"),
    (r"contingent\s+value", "CVR"),
    (r"\bordinary\s+shares?\b", "Ordinary Shares"),
    (r"american\s+depositary", "ADR"),
    (r"\bcommon\s+stock\b|\bclass\s+[a-z]\b|\bcommon\s+shares?\b", "Common Stock"),
]

_FUND_PATTERNS = re.compile(
    r"\betf\b|\betn\b|\bexchange[- ]traded\b|\bindex\s+fund\b|"
    r"\bclosed[- ]end\b|\bmunicipal\s+income\b|\bincome\s+fund\b|"
    r"\bcapital\s+fund\b|\bequity\s+fund\b|\bportfolio\b",
    re.IGNORECASE,
)

_SPAC_PATTERNS = re.compile(
    r"\bacquisition\s+corp|\bacquisition\s+co\b|\bacquisition\s+holdings\b|"
    r"\bblank\s+check\b",
    re.IGNORECASE,
)


def classify_security(security_name: str, etf_flag: str) -> str:
    name = security_name or ""
    tail = name.split(" - ", 1)[1] if " - " in name else name
    if (etf_flag or "").upper() == "Y":
        return "ETF/ETN"
    for pattern, label in _CLASS_RULES:
        if re.search(pattern, tail, re.IGNORECASE):
            return label
    if _FUND_PATTERNS.search(name):
        return "Fund"
    return "Other"


def issuer_name(security_name: str) -> str:
    return (security_name or "").split(" - ", 1)[0].strip()


def parse_pipe_file(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text), delimiter="|"))
    # The last line of every Nasdaq Trader file is a footer timestamp.
    return [r for r in rows if r.get(list(r.keys())[0]) and "File Creation Time" not in str(r.get(list(r.keys())[0]))]


def ticker_variants(symbol: str) -> list[str]:
    """
    Nasdaq writes share classes as BRK.A or BRK/A; the SEC writes BRK-B.
    Try every spelling so we don't silently lose dual-class names.
    """
    s = symbol.strip().upper()
    out = [s, s.replace(".", "-"), s.replace("/", "-"), s.replace(".", ""), s.replace("/", "")]
    seen, uniq = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def load_sec_cik_map(client) -> dict[str, dict]:
    """ticker -> {cik, title, exchange} using the SEC's own mapping files."""
    mapping: dict[str, dict] = {}

    data = client.get_json(SEC_TICKERS_EXCHANGE_URL, cache_ttl=86_400, allow_404=True)
    if data and "fields" in data and "data" in data:
        idx = {name: i for i, name in enumerate(data["fields"])}
        for row in data["data"]:
            ticker = str(row[idx["ticker"]]).strip().upper()
            if not ticker:
                continue
            mapping[ticker] = {
                "cik": int(row[idx["cik"]]),
                "sec_name": row[idx["name"]],
                "sec_exchange": row[idx.get("exchange", 0)] or "",
            }
        log.info("SEC company_tickers_exchange.json: %d tickers", len(mapping))

    # Fall back / top up from the simpler file, which occasionally has
    # tickers the exchange file is missing.
    simple = client.get_json(SEC_TICKERS_URL, cache_ttl=86_400, allow_404=True)
    if simple:
        added = 0
        for entry in simple.values():
            ticker = str(entry.get("ticker", "")).strip().upper()
            if ticker and ticker not in mapping:
                mapping[ticker] = {
                    "cik": int(entry["cik_str"]),
                    "sec_name": entry.get("title", ""),
                    "sec_exchange": "",
                }
                added += 1
        log.info("company_tickers.json added %d further tickers", added)

    return mapping


def main() -> int:
    client = make_sec_client()
    # Nasdaq Trader is a separate host with its own tolerance. Two requests
    # a second is more than polite for two files.
    client.set_rate("www.nasdaqtrader.com", 2.0)

    log.info("Downloading Nasdaq Trader symbol directories ...")
    records: dict[str, dict] = {}

    for row in parse_pipe_file(client.get(NASDAQ_LISTED_URL, cache_ttl=21_600)):
        symbol = (row.get("Symbol") or "").strip()
        if not symbol:
            continue
        records[symbol] = {
            "Ticker": symbol,
            "Security Name": (row.get("Security Name") or "").strip(),
            "Exchange": "NASDAQ",
            "Market Tier": (row.get("Market Category") or "").strip(),
            "ETF Flag": (row.get("ETF") or "N").strip(),
            "Test Issue": (row.get("Test Issue") or "N").strip(),
            "Financial Status": FINANCIAL_STATUS.get(
                (row.get("Financial Status") or "").strip(), ""
            ),
            "Round Lot": (row.get("Round Lot Size") or "").strip(),
        }

    for row in parse_pipe_file(client.get(OTHER_LISTED_URL, cache_ttl=21_600)):
        symbol = (row.get("ACT Symbol") or "").strip()
        if not symbol:
            continue
        code = (row.get("Exchange") or "").strip()
        records[symbol] = {
            "Ticker": symbol,
            "Security Name": (row.get("Security Name") or "").strip(),
            "Exchange": EXCHANGE_CODES.get(code, code),
            "Market Tier": "",
            "ETF Flag": (row.get("ETF") or "N").strip(),
            "Test Issue": (row.get("Test Issue") or "N").strip(),
            "Financial Status": "",
            "Round Lot": (row.get("Round Lot Size") or "").strip(),
        }

    log.info("%d raw symbols across both exchanges files", len(records))

    cik_map = load_sec_cik_map(client)

    rows = []
    matched = 0
    for symbol, rec in sorted(records.items()):
        if rec["Test Issue"].upper() == "Y":
            continue

        hit = None
        for variant in ticker_variants(symbol):
            if variant in cik_map:
                hit = cik_map[variant]
                break
        if hit:
            matched += 1

        rows.append(
            {
                "Ticker": symbol,
                "Issuer Name": issuer_name(rec["Security Name"]),
                "Security Name": rec["Security Name"],
                "Security Type": classify_security(rec["Security Name"], rec["ETF Flag"]),
                "Exchange": rec["Exchange"],
                "Market Tier": rec["Market Tier"],
                "Financial Status": rec["Financial Status"],
                "Likely SPAC": "Y" if _SPAC_PATTERNS.search(rec["Security Name"]) else "",
                "CIK": hit["cik"] if hit else "",
                "SEC Name": hit["sec_name"] if hit else "",
                "SEC Exchange": hit["sec_exchange"] if hit else "",
            }
        )

    out = DATA_DIR / "universe.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["Security Type"]] = by_type.get(r["Security Type"], 0) + 1

    log.info("Wrote %s  (%d rows, %d matched to a CIK)", out, len(rows), matched)
    log.info("Breakdown by security type: %s", dict(sorted(by_type.items())))
    log.info(client.stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())
