"""
fetch_us_stock_universe.py

Builds a CSV of all US-exchange-listed common stocks (excluding ETFs,
funds, warrants, rights, units, preferred shares, and other non-operating-
company securities), enriched with industry, exchange, state of
incorporation, and other attributes -- pulled entirely from free public
sources:

  1. Nasdaq Trader symbol directories (nasdaqlisted.txt / otherlisted.txt)
     -> base list of every actively-traded US exchange symbol, with an
        explicit ETF flag we use to exclude funds.
  2. SEC EDGAR company_tickers.json
     -> maps each ticker to a CIK (SEC's company ID number).
  3. SEC EDGAR submissions API (data.sec.gov/submissions/CIK##########.json)
     -> pulls SIC code/industry, state of incorporation, fiscal year end,
        former names, and exchange(s) of record for each company.
  4. (Optional) Alpha Vantage LISTING_STATUS bulk endpoint
     -> adds IPO/first-listed date. Requires a free API key
        (https://www.alphavantage.co/support/#api-key). Skipped if no
        key is set.

No paid services, no paid API tiers. Pure Python standard library --
no pip installs required unless you want the bonus .xlsx output.

Run:    python3 fetch_us_stock_universe.py
Output: us_listed_companies.csv  (and .xlsx if pandas+openpyxl installed)

Config via environment variables:
  SEC_USER_AGENT          e.g. "Jane Doe jane@example.com"  (required by SEC)
  ALPHA_VANTAGE_API_KEY   optional, enables IPO date column
  MAX_COMPANIES           optional int, limits run size for quick testing
"""

import csv
import io
import json
import os
import sys
import time
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# SEC requires a descriptive User-Agent identifying you/your app + contact
# email. Generic or blank User-Agents get blocked. Replace the default below
# (or set the SEC_USER_AGENT environment variable / GitHub secret).
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "YourName YourEmail@example.com")

# Optional. Get a free key at https://www.alphavantage.co/support/#api-key
# Leave unset to skip IPO-date enrichment (everything else still works).
ALPHA_VANTAGE_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "")

# Optional. Set to a small number (e.g. "50") while testing so you don't
# wait ~45 minutes for a full run.
MAX_COMPANIES = os.environ.get("MAX_COMPANIES")
MAX_COMPANIES = int(MAX_COMPANIES) if MAX_COMPANIES else None

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ALPHA_VANTAGE_LISTING_URL = (
    "https://www.alphavantage.co/query?function=LISTING_STATUS&apikey={key}"
)

OUTPUT_CSV = "us_listed_companies.csv"

# Keywords in the security name that indicate it's NOT a plain operating
# company (funds, notes, warrants, rights, units, preferred stock, SPAC
# placeholders, etc). Matched against a lowercased, padded copy of the name.
# Edit this list to taste -- it's the main lever for what counts as
# "an actual company" for your purposes.
EXCLUDE_NAME_KEYWORDS = [
    " fund", " trust", " etf", " etn", "exchange traded", " notes",
    "warrant", "warrants", " right", " rights", " unit ", " units",
    "preferred", "depositary", "acquisition corp", "acquisition corp.",
    " spac", " index",
]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def http_get(url, headers=None, retries=3, backoff=2.0):
    headers = headers or {}
    last_err = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last_err = e
            log(f"  request failed ({e}); attempt {attempt}/{retries}")
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise last_err


# ---------------------------------------------------------------------------
# STEP 1: Nasdaq Trader symbol directories -> base universe + ETF flag
# ---------------------------------------------------------------------------

def load_nasdaq_symbol_directory():
    log("Downloading Nasdaq Trader symbol directories...")
    rows = {}

    raw = http_get(NASDAQ_LISTED_URL).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(raw), delimiter="|")
    for r in reader:
        symbol = (r.get("Symbol") or "").strip()
        if not symbol or "File Creation Time" in symbol:
            continue
        rows[symbol] = {
            "Ticker": symbol,
            "Security Name": (r.get("Security Name") or "").strip(),
            "Exchange": "NASDAQ",
            "ETF": (r.get("ETF") or "N").strip(),
            "Test Issue": (r.get("Test Issue") or "N").strip(),
        }

    raw = http_get(OTHER_LISTED_URL).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(raw), delimiter="|")
    exch_map = {
        "A": "NYSE American", "N": "NYSE", "P": "NYSE Arca",
        "Z": "Cboe BZX", "V": "IEXG",
    }
    for r in reader:
        symbol = (r.get("ACT Symbol") or "").strip()
        if not symbol or "File Creation Time" in symbol:
            continue
        rows[symbol] = {
            "Ticker": symbol,
            "Security Name": (r.get("Security Name") or "").strip(),
            "Exchange": exch_map.get((r.get("Exchange") or "").strip(),
                                      (r.get("Exchange") or "").strip()),
            "ETF": (r.get("ETF") or "N").strip(),
            "Test Issue": (r.get("Test Issue") or "N").strip(),
        }

    log(f"  {len(rows)} raw symbols found across both files.")
    return rows


def is_probably_a_company(security_name, etf_flag, test_issue_flag):
    if etf_flag.upper() == "Y":
        return False
    if test_issue_flag.upper() == "Y":
        return False
    name_lower = f" {security_name.lower()} "
    for kw in EXCLUDE_NAME_KEYWORDS:
        if kw in name_lower:
            return False
    return True


# ---------------------------------------------------------------------------
# STEP 2: SEC company_tickers.json -> ticker -> CIK map
# ---------------------------------------------------------------------------

def load_sec_ticker_cik_map():
    log("Downloading SEC ticker/CIK map...")
    raw = http_get(SEC_TICKERS_URL, headers={"User-Agent": SEC_USER_AGENT})
    data = json.loads(raw)
    mapping = {}
    for entry in data.values():
        ticker = (entry.get("ticker") or "").strip().upper()
        cik = entry.get("cik_str")
        if ticker and cik is not None:
            mapping[ticker] = int(cik)
    log(f"  {len(mapping)} tickers mapped to CIKs.")
    return mapping


# ---------------------------------------------------------------------------
# STEP 3: SEC submissions API -> industry / state / fiscal year end / etc.
# ---------------------------------------------------------------------------

def fetch_company_detail(cik):
    url = SEC_SUBMISSIONS_URL.format(cik=cik)
    raw = http_get(url, headers={"User-Agent": SEC_USER_AGENT})
    data = json.loads(raw)
    exchanges = data.get("exchanges") or []
    former_names = [n.get("name") for n in data.get("formerNames", []) if n.get("name")]
    return {
        "CIK": cik,
        "Legal Name (SEC)": data.get("name", ""),
        "SIC Code": data.get("sic", ""),
        "Industry (SIC Description)": data.get("sicDescription", ""),
        "State of Incorporation": data.get("stateOfIncorporation", ""),
        "Fiscal Year End": data.get("fiscalYearEnd", ""),
        "SEC Exchange(s)": ", ".join(exchanges),
        "Former Name(s)": "; ".join(former_names),
    }


# ---------------------------------------------------------------------------
# STEP 4 (optional): Alpha Vantage LISTING_STATUS -> IPO date
# ---------------------------------------------------------------------------

def load_alpha_vantage_listing_status():
    if not ALPHA_VANTAGE_API_KEY:
        log("No ALPHA_VANTAGE_API_KEY set -- skipping IPO-date enrichment.")
        return {}
    log("Downloading Alpha Vantage LISTING_STATUS...")
    url = ALPHA_VANTAGE_LISTING_URL.format(key=ALPHA_VANTAGE_API_KEY)
    raw = http_get(url).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    out = {}
    for r in reader:
        sym = (r.get("symbol") or "").strip().upper()
        if sym:
            out[sym] = {
                "IPO/First Listed Date": r.get("ipoDate", ""),
                "Alpha Vantage Asset Type": r.get("assetType", ""),
                "Status": r.get("status", ""),
            }
    log(f"  {len(out)} symbols returned by Alpha Vantage.")
    return out


# ---------------------------------------------------------------------------
# PIPELINE (importable so a test harness can feed it mock data)
# ---------------------------------------------------------------------------

FIELDNAMES = [
    "Ticker", "Company Name", "Exchange", "Industry (SIC Description)",
    "SIC Code", "CIK", "State of Incorporation", "Fiscal Year End",
    "IPO/First Listed Date", "Former Name(s)", "SEC Exchange(s)",
    "Alpha Vantage Asset Type", "Status",
]


def build_rows(nasdaq_dir, cik_map, av_listing, detail_fetcher=fetch_company_detail,
               sleep_seconds=0.11, max_companies=None):
    companies = []
    for symbol, rec in nasdaq_dir.items():
        if not is_probably_a_company(rec["Security Name"], rec["ETF"], rec["Test Issue"]):
            continue
        companies.append(rec)

    log(f"{len(companies)} symbols look like real operating companies "
        f"(post ETF/fund/warrant/etc. filtering).")

    if max_companies:
        companies = companies[:max_companies]

    results = []
    total = len(companies)
    for i, rec in enumerate(companies, 1):
        symbol = rec["Ticker"]
        row = {
            "Ticker": symbol,
            "Company Name": rec["Security Name"],
            "Exchange": rec["Exchange"],
        }

        cik = cik_map.get(symbol)
        if cik is not None:
            try:
                detail = detail_fetcher(cik)
                row.update({
                    "Company Name": detail["Legal Name (SEC)"] or row["Company Name"],
                    "Industry (SIC Description)": detail["Industry (SIC Description)"],
                    "SIC Code": detail["SIC Code"],
                    "CIK": detail["CIK"],
                    "State of Incorporation": detail["State of Incorporation"],
                    "Fiscal Year End": detail["Fiscal Year End"],
                    "Former Name(s)": detail["Former Name(s)"],
                    "SEC Exchange(s)": detail["SEC Exchange(s)"],
                })
            except Exception as e:
                log(f"  [{i}/{total}] SEC lookup failed for {symbol}: {e}")
            if sleep_seconds:
                time.sleep(sleep_seconds)  # stay under SEC's ~10 req/sec limit

        av = av_listing.get(symbol)
        if av:
            row.update(av)

        results.append(row)
        if i % 250 == 0 or i == total:
            log(f"  processed {i}/{total} ({symbol})")

    results.sort(key=lambda r: r["Ticker"])
    return results


def write_csv(results, path=OUTPUT_CSV):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    log(f"Wrote {len(results)} companies to {path}")


def write_xlsx_if_possible(path=OUTPUT_CSV):
    try:
        import pandas as pd
        df = pd.read_csv(path)
        xlsx_path = path.replace(".csv", ".xlsx")
        df.to_excel(xlsx_path, index=False)
        log(f"Wrote {xlsx_path}")
    except ImportError:
        log("pandas/openpyxl not installed -- skipped .xlsx output "
            "(Excel opens the CSV directly too, no conversion needed).")


def main():
    nasdaq_dir = load_nasdaq_symbol_directory()
    cik_map = load_sec_ticker_cik_map()
    av_listing = load_alpha_vantage_listing_status()

    results = build_rows(nasdaq_dir, cik_map, av_listing, max_companies=MAX_COMPANIES)

    write_csv(results)
    write_xlsx_if_possible()


if __name__ == "__main__":
    main()
