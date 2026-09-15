"""
fetch_us_stock_universe.py

Builds a CSV of US-exchange-listed operating companies and enriches them
with SEC EDGAR data.

Data sources:
  1. Nasdaq Trader symbol directories
     -> base list of US-listed securities
     -> ETF/test-issue filtering
  2. SEC company_tickers.json
     -> ticker -> CIK mapping
  3. SEC EDGAR submissions API
     -> company name, SIC, incorporation state, fiscal year end,
        former names, exchange(s), filing history
  4. SEC EDGAR registration filings
     -> SEC Public Date, ONLY when an explicit public/listing date
        can be identified in the SEC filing text

IMPORTANT:
  The SEC does not maintain a universal structured IPO-date field.
  Therefore this script NEVER assumes that the first SEC filing date
  is the IPO/public date.

  SEC Public Date is populated only when an SEC registration filing
  contains sufficiently explicit language identifying a public/listing
  date. Otherwise the field is blank.

No Alpha Vantage dependency.
No paid services.
Pure Python standard library.

Run:
    python3 fetch_us_stock_universe.py

Outputs:
    us_listed_companies.csv
    us_listed_companies.xlsx (if pandas/openpyxl are installed)

Environment variables:
    SEC_USER_AGENT
        Required/preferred. Example:
        "Jane Doe jane@example.com"

    MAX_COMPANIES
        Optional integer for testing.
        Example: 50

    SEC_REQUEST_DELAY
        Optional seconds between SEC requests.
        Default: 0.12

    SEC_PUBLIC_DATE_LOOKUP
        Optional:
            "1" = inspect SEC registration filings
            "0" = skip SEC public-date lookup

        Default: "1"
"""

import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "YourName YourEmail@example.com"
)

MAX_COMPANIES = os.environ.get("MAX_COMPANIES")
MAX_COMPANIES = int(MAX_COMPANIES) if MAX_COMPANIES else None

SEC_REQUEST_DELAY = float(
    os.environ.get("SEC_REQUEST_DELAY", "0.12")
)

SEC_PUBLIC_DATE_LOOKUP = os.environ.get(
    "SEC_PUBLIC_DATE_LOOKUP",
    "1"
).strip() not in {"0", "false", "False", "no", "NO"}


NASDAQ_LISTED_URL = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
)

OTHER_LISTED_URL = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
)

SEC_TICKERS_URL = (
    "https://www.sec.gov/files/company_tickers.json"
)

SEC_SUBMISSIONS_URL = (
    "https://data.sec.gov/submissions/CIK{cik:010d}.json"
)

SEC_ARCHIVES_BASE = (
    "https://www.sec.gov/Archives/edgar/data"
)

OUTPUT_CSV = "us_listed_companies.csv"


# ---------------------------------------------------------------------------
# SECURITY FILTERING
# ---------------------------------------------------------------------------

EXCLUDE_NAME_KEYWORDS = [
    " fund",
    " trust",
    " etf",
    " etn",
    "exchange traded",
    " notes",
    "warrant",
    "warrants",
    " right",
    " rights",
    " unit ",
    " units",
    "preferred",
    "depositary",
    "acquisition corp",
    "acquisition corp.",
    " spac",
    " index",
]


# Registration statement forms that are most useful for finding
# an initial public/listing date.
REGISTRATION_FORMS = {
    "S-1",
    "S-1/A",
    "F-1",
    "F-1/A",
    "SB-1",
    "SB-1/A",
    "SB-2",
    "SB-2/A",
}


# ---------------------------------------------------------------------------
# LOGGING / HTTP
# ---------------------------------------------------------------------------

def log(msg):
    print(msg, file=sys.stderr, flush=True)


def http_get(url, headers=None, retries=3, backoff=2.0):
    """
    Download a URL with retries.

    SEC requests always receive the configured User-Agent.
    """
    headers = headers or {}

    last_err = None

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()

        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last_err = e

            log(
                f"  request failed ({e}); "
                f"attempt {attempt}/{retries}"
            )

            if attempt < retries:
                time.sleep(backoff * attempt)

    raise last_err


def sec_get(url):
    """
    SEC-specific GET request with fair-access pacing.
    """
    raw = http_get(
        url,
        headers={
            "User-Agent": SEC_USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Host": urllib.request.urlparse(url).netloc,
        },
    )

    if SEC_REQUEST_DELAY > 0:
        time.sleep(SEC_REQUEST_DELAY)

    return raw


# ---------------------------------------------------------------------------
# STEP 1:
# NASDAQ TRADER SYMBOL DIRECTORIES
# ---------------------------------------------------------------------------

def load_nasdaq_symbol_directory():
    log("Downloading Nasdaq Trader symbol directories...")

    rows = {}

    # -------------------------
    # NASDAQ-listed securities
    # -------------------------

    raw = http_get(NASDAQ_LISTED_URL).decode(
        "utf-8",
        errors="replace"
    )

    reader = csv.DictReader(
        io.StringIO(raw),
        delimiter="|"
    )

    for r in reader:
        symbol = (r.get("Symbol") or "").strip()

        if not symbol or "File Creation Time" in symbol:
            continue

        rows[symbol] = {
            "Ticker": symbol,
            "Security Name": (
                r.get("Security Name") or ""
            ).strip(),
            "Exchange": "NASDAQ",
            "ETF": (
                r.get("ETF") or "N"
            ).strip(),
            "Test Issue": (
                r.get("Test Issue") or "N"
            ).strip(),
        }

    # -------------------------
    # Other US-listed securities
    # -------------------------

    raw = http_get(OTHER_LISTED_URL).decode(
        "utf-8",
        errors="replace"
    )

    reader = csv.DictReader(
        io.StringIO(raw),
        delimiter="|"
    )

    exch_map = {
        "A": "NYSE American",
        "N": "NYSE",
        "P": "NYSE Arca",
        "Z": "Cboe BZX",
        "V": "IEXG",
    }

    for r in reader:
        symbol = (r.get("ACT Symbol") or "").strip()

        if not symbol or "File Creation Time" in symbol:
            continue

        rows[symbol] = {
            "Ticker": symbol,
            "Security Name": (
                r.get("Security Name") or ""
            ).strip(),
            "Exchange": exch_map.get(
                (r.get("Exchange") or "").strip(),
                (r.get("Exchange") or "").strip()
            ),
            "ETF": (
                r.get("ETF") or "N"
            ).strip(),
            "Test Issue": (
                r.get("Test Issue") or "N"
            ).strip(),
        }

    log(
        f"  {len(rows)} raw symbols found across both files."
    )

    return rows


def is_probably_a_company(
    security_name,
    etf_flag,
    test_issue_flag
):
    """
    Heuristic filter for operating companies.

    This intentionally retains the behavior of the original repository.
    """

    if etf_flag.upper() == "Y":
        return False

    if test_issue_flag.upper() == "Y":
        return False

    name_lower = f" {security_name.lower()} "

    for keyword in EXCLUDE_NAME_KEYWORDS:
        if keyword in name_lower:
            return False

    return True


# ---------------------------------------------------------------------------
# STEP 2:
# SEC TICKER -> CIK
# ---------------------------------------------------------------------------

def load_sec_ticker_cik_map():
    log("Downloading SEC ticker/CIK map...")

    raw = sec_get(SEC_TICKERS_URL)

    data = json.loads(raw)

    mapping = {}

    for entry in data.values():
        ticker = (
            entry.get("ticker") or ""
        ).strip().upper()

        cik = entry.get("cik_str")

        if ticker and cik is not None:
            mapping[ticker] = int(cik)

    log(
        f"  {len(mapping)} tickers mapped to CIKs."
    )

    return mapping


# ---------------------------------------------------------------------------
# STEP 3:
# SEC SUBMISSIONS DATA
# ---------------------------------------------------------------------------

def fetch_sec_submissions(cik):
    url = SEC_SUBMISSIONS_URL.format(cik=cik)

    raw = sec_get(url)

    return json.loads(raw)


def fetch_company_detail(cik):
    """
    Fetch the company's SEC submissions metadata and determine its
    SEC-derived public date.

    The public date is deliberately conservative:
    it is blank unless an explicit date can be identified in an
    appropriate SEC registration filing.
    """

    data = fetch_sec_submissions(cik)

    exchanges = data.get("exchanges") or []

    former_names = [
        n.get("name")
        for n in data.get("formerNames", [])
        if n.get("name")
    ]

    public_date = ""

    if SEC_PUBLIC_DATE_LOOKUP:
        try:
            public_date = find_sec_public_date(
                cik,
                data
            )
        except Exception as e:
            log(
                f"    SEC public-date lookup failed for "
                f"CIK {cik}: {e}"
            )

    return {
        "CIK": cik,
        "Legal Name (SEC)": data.get("name", ""),
        "SIC Code": data.get("sic", ""),
        "Industry (SIC Description)": data.get(
            "sicDescription",
            ""
        ),
        "State of Incorporation": data.get(
            "stateOfIncorporation",
            ""
        ),
        "Fiscal Year End": data.get(
            "fiscalYearEnd",
            ""
        ),
        "SEC Exchange(s)": ", ".join(exchanges),
        "Former Name(s)": "; ".join(former_names),
        "SEC Public Date": public_date,
    }


# ---------------------------------------------------------------------------
# STEP 4:
# SEC REGISTRATION FILINGS
# ---------------------------------------------------------------------------

def get_all_submission_rows(cik, submissions):
    """
    Return filing-history rows from the current submissions JSON and
    any historical JSON files referenced by the submissions API.

    The SEC submissions API can split older filing history into
    separate JSON files.
    """

    rows = []

    recent = submissions.get("filings", {}).get(
        "recent",
        {}
    )

    if recent:
        rows.extend(
            columnar_rows_to_dicts(recent)
        )

    historical_files = (
        submissions.get("filings", {}).get(
            "files",
            []
        )
    )

    for file_info in historical_files:
        name = file_info.get("name")

        if not name:
            continue

        url = (
            "https://data.sec.gov/submissions/"
            + name
        )

        try:
            raw = sec_get(url)

            historical = json.loads(raw)

            rows.extend(
                columnar_rows_to_dicts(historical)
            )

        except Exception as e:
            log(
                f"    Could not load SEC historical "
                f"submission file {name}: {e}"
            )

    return rows


def columnar_rows_to_dicts(data):
    """
    Convert the SEC submissions API's columnar recent-filing
    structure into normal dictionaries.
    """

    if not data:
        return []

    # Historical files use the same structure:
    # {
    #   "accessionNumber": [...],
    #   "filingDate": [...],
    #   ...
    # }

    keys = list(data.keys())

    if not keys:
        return []

    lengths = []

    for key in keys:
        value = data.get(key)

        if isinstance(value, list):
            lengths.append(len(value))

    if not lengths:
        return []

    count = max(lengths)

    rows = []

    for i in range(count):
        row = {}

        for key in keys:
            value = data.get(key)

            if isinstance(value, list):
                row[key] = (
                    value[i]
                    if i < len(value)
                    else ""
                )
            else:
                row[key] = value

        rows.append(row)

    return rows


def normalize_accession(accession):
    return (
        (accession or "")
        .replace("-", "")
        .strip()
    )


def registration_filings(cik, submissions):
    """
    Find SEC registration filings that could contain an explicit
    public/listing date.
    """

    rows = get_all_submission_rows(
        cik,
        submissions
    )

    candidates = []

    for row in rows:
        form = (
            row.get("form") or ""
        ).strip().upper()

        if form not in REGISTRATION_FORMS:
            continue

        accession = normalize_accession(
            row.get("accessionNumber")
        )

        primary_document = (
            row.get("primaryDocument") or ""
        ).strip()

        filing_date = (
            row.get("filingDate") or ""
        ).strip()

        if not accession or not primary_document:
            continue

        candidates.append({
            "form": form,
            "accession": accession,
            "primaryDocument": primary_document,
            "filingDate": filing_date,
        })

    # Oldest registration statement first.
    candidates.sort(
        key=lambda x: x.get("filingDate") or "9999-99-99"
    )

    return candidates


def build_sec_filing_url(
    cik,
    accession,
    primary_document
):
    return (
        f"{SEC_ARCHIVES_BASE}/"
        f"{int(cik)}/"
        f"{accession}/"
        f"{primary_document}"
    )


def strip_html(html):
    """
    Very lightweight HTML-to-text conversion.

    We don't need a full HTML parser for the relatively simple
    extraction we're doing.
    """

    text = html.decode(
        "utf-8",
        errors="replace"
    )

    text = re.sub(
        r"(?is)<script.*?</script>",
        " ",
        text
    )

    text = re.sub(
        r"(?is)<style.*?</style>",
        " ",
        text
    )

    text = re.sub(
        r"(?is)<[^>]+>",
        " ",
        text
    )

    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# ---------------------------------------------------------------------------
# DATE EXTRACTION
# ---------------------------------------------------------------------------

MONTH_PATTERN = (
    r"(?:"
    r"January|February|March|April|May|June|"
    r"July|August|September|October|November|December"
    r")"
)

DATE_PATTERN = (
    rf"(?:{MONTH_PATTERN})\s+"
    r"\d{1,2},\s+\d{4}"
)


def parse_date_string(value):
    """
    Convert a textual US date to YYYY-MM-DD.

    Returns "" if parsing fails.
    """

    value = (value or "").strip()

    formats = [
        "%B %d, %Y",
        "%b %d, %Y",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(
                value,
                fmt
            )

            return dt.strftime("%Y-%m-%d")

        except ValueError:
            pass

    return ""


def find_explicit_public_date(text):
    """
    Look for unusually explicit SEC language identifying a
    public/listing date.

    We intentionally DO NOT interpret:
      - filing date
      - incorporation date
      - fiscal year end
      - date of prospectus
      - registration effectiveness date alone

    as the public date.

    Returns:
        YYYY-MM-DD or ""
    """

    if not text:
        return ""

    # Normalize whitespace but preserve sentence structure reasonably.
    text = re.sub(
        r"\s+",
        " ",
        text
    ).strip()

    # ------------------------------------------------------------------
    # HIGH-CONFIDENCE PHRASES
    # ------------------------------------------------------------------
    #
    # These patterns intentionally require wording that relates the
    # date directly to becoming a public company / trading publicly.
    #
    # We do NOT simply search for "public" and a date because prospectuses
    # contain thousands of unrelated dates.
    # ------------------------------------------------------------------

    patterns = [
        # "became a public company on January 1, 2020"
        rf"\bbecame\s+(?:a\s+)?public\s+company\s+"
        rf"(?:on|as\s+of)\s+({DATE_PATTERN})",

        # "became public on January 1, 2020"
        rf"\bbecame\s+public\s+"
        rf"(?:on|as\s+of)\s+({DATE_PATTERN})",

        # "became a publicly traded company on..."
        rf"\bbecame\s+a\s+publicly\s+traded\s+company\s+"
        rf"(?:on|as\s+of)\s+({DATE_PATTERN})",

        # "first became publicly traded on..."
        rf"\bfirst\s+became\s+publicly\s+traded\s+"
        rf"(?:on|as\s+of)\s+({DATE_PATTERN})",

        # "first traded publicly on..."
        rf"\bfirst\s+traded\s+publicly\s+"
        rf"(?:on|as\s+of)\s+({DATE_PATTERN})",

        # "shares began trading publicly on..."
        rf"\bshares?\s+began\s+trading\s+"
        rf"(?:publicly\s+)?(?:on|as\s+of)\s+({DATE_PATTERN})",

        # "common stock began trading on..."
        rf"\b(?:common\s+stock|shares?)\s+"
        rf"began\s+trading\s+"
        rf"(?:on|as\s+of)\s+({DATE_PATTERN})",

        # "initial public offering was on..."
        rf"\binitial\s+public\s+offering\s+"
        rf"(?:was|occurred|took\s+place)\s+"
        rf"(?:on|as\s+of)\s+({DATE_PATTERN})",

        # "IPO occurred on..."
        rf"\bIPO\s+"
        rf"(?:was|occurred|took\s+place)\s+"
        rf"(?:on|as\s+of)\s+({DATE_PATTERN})",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:
            parsed = parse_date_string(
                match.group(1)
            )

            if parsed:
                return parsed

    # ------------------------------------------------------------------
    # "TRADING BEGAN" PATTERNS
    # ------------------------------------------------------------------

    trading_patterns = [
        rf"\btrading\s+of\s+(?:our\s+)?"
        rf"(?:common\s+stock|shares?)\s+"
        rf"began\s+on\s+({DATE_PATTERN})",

        rf"\btrading\s+in\s+(?:our\s+)?"
        rf"(?:common\s+stock|shares?)\s+"
        rf"began\s+on\s+({DATE_PATTERN})",

        rf"\b(?:common\s+stock|shares?)\s+"
        rf"commenced\s+trading\s+on\s+({DATE_PATTERN})",
    ]

    for pattern in trading_patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:
            parsed = parse_date_string(
                match.group(1)
            )

            if parsed:
                return parsed

    return ""


# ---------------------------------------------------------------------------
# SEC PUBLIC DATE LOOKUP
# ---------------------------------------------------------------------------

def find_sec_public_date(cik, submissions):
    """
    Search SEC registration statements for an explicit public/listing date.

    Important:
        We inspect registration filings only.

    If no explicit date is found:
        return ""

    We never use:
        - first SEC filing date
        - incorporation date
        - registration effectiveness date alone
        - prospectus date alone
    """

    candidates = registration_filings(
        cik,
        submissions
    )

    if not candidates:
        return ""

    # To avoid downloading huge numbers of documents for companies
    # with many registration statements, inspect the oldest few
    # registration filings first.

    # Usually the initial registration statement is among these.
    candidates = candidates[:8]

    for filing in candidates:
        accession = filing["accession"]
        primary_document = filing["primaryDocument"]

        url = build_sec_filing_url(
            cik,
            accession,
            primary_document
        )

        try:
            raw = sec_get(url)

            text = strip_html(raw)

            public_date = find_explicit_public_date(
                text
            )

            if public_date:
                return public_date

        except Exception as e:
            log(
                f"    Could not inspect "
                f"{filing['form']} "
                f"{accession}: {e}"
            )

    return ""


# ---------------------------------------------------------------------------
# PIPELINE
# ---------------------------------------------------------------------------

FIELDNAMES = [
    "Ticker",
    "Company Name",
    "Exchange",
    "Industry (SIC Description)",
    "SIC Code",
    "CIK",
    "State of Incorporation",
    "Fiscal Year End",
    "SEC Public Date",
    "Former Name(s)",
    "SEC Exchange(s)",
]


def build_rows(
    nasdaq_dir,
    cik_map,
    detail_fetcher=fetch_company_detail,
    sleep_seconds=0.11,
    max_companies=None,
):
    companies = []

    for symbol, rec in nasdaq_dir.items():

        if not is_probably_a_company(
            rec["Security Name"],
            rec["ETF"],
            rec["Test Issue"]
        ):
            continue

        companies.append(rec)

    log(
        f"{len(companies)} symbols look like real operating "
        f"companies (post ETF/fund/warrant/etc. filtering)."
    )

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
            "Industry (SIC Description)": "",
            "SIC Code": "",
            "CIK": "",
            "State of Incorporation": "",
            "Fiscal Year End": "",
            "SEC Public Date": "",
            "Former Name(s)": "",
            "SEC Exchange(s)": "",
        }

        cik = cik_map.get(symbol)

        if cik is not None:

            try:
                detail = detail_fetcher(cik)

                row.update({
                    "Company Name": (
                        detail["Legal Name (SEC)"]
                        or row["Company Name"]
                    ),

                    "Industry (SIC Description)": (
                        detail["Industry (SIC Description)"]
                    ),

                    "SIC Code": (
                        detail["SIC Code"]
                    ),

                    "CIK": (
                        detail["CIK"]
                    ),

                    "State of Incorporation": (
                        detail["State of Incorporation"]
                    ),

                    "Fiscal Year End": (
                        detail["Fiscal Year End"]
                    ),

                    "SEC Public Date": (
                        detail["SEC Public Date"]
                    ),

                    "Former Name(s)": (
                        detail["Former Name(s)"]
                    ),

                    "SEC Exchange(s)": (
                        detail["SEC Exchange(s)"]
                    ),
                })

            except Exception as e:

                log(
                    f"  [{i}/{total}] SEC lookup failed "
                    f"for {symbol}: {e}"
                )

            if sleep_seconds:
                time.sleep(sleep_seconds)

        results.append(row)

        if i % 100 == 0 or i == total:
            log(
                f"  processed {i}/{total} ({symbol})"
            )

    results.sort(
        key=lambda r: r["Ticker"]
    )

    return results


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------

def write_csv(
    results,
    path=OUTPUT_CSV
):
    with open(
        path,
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=FIELDNAMES,
            extrasaction="ignore"
        )

        writer.writeheader()
        writer.writerows(results)

    populated = sum(
        1
        for row in results
        if row.get("SEC Public Date")
    )

    log(
        f"Wrote {len(results)} companies to {path}"
    )

    log(
        f"SEC Public Date populated for "
        f"{populated}/{len(results)} companies."
    )


def write_xlsx_if_possible(
    path=OUTPUT_CSV
):
    try:

        import pandas as pd

        df = pd.read_csv(path)

        xlsx_path = path.replace(
            ".csv",
            ".xlsx"
        )

        df.to_excel(
            xlsx_path,
            index=False
        )

        log(
            f"Wrote {xlsx_path}"
        )

    except ImportError:

        log(
            "pandas/openpyxl not installed -- "
            "skipped .xlsx output."
        )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():

    log("=" * 70)
    log("US STOCK UNIVERSE REFRESH")
    log("=" * 70)

    log(
        f"SEC public-date lookup: "
        f"{'ENABLED' if SEC_PUBLIC_DATE_LOOKUP else 'DISABLED'}"
    )

    log(
        f"SEC request delay: "
        f"{SEC_REQUEST_DELAY}s"
    )

    if MAX_COMPANIES:
        log(
            f"MAX_COMPANIES: {MAX_COMPANIES}"
        )

    log("")

    nasdaq_dir = (
        load_nasdaq_symbol_directory()
    )

    cik_map = (
        load_sec_ticker_cik_map()
    )

    results = build_rows(
        nasdaq_dir,
        cik_map,
        max_companies=MAX_COMPANIES
    )

    write_csv(results)

    write_xlsx_if_possible()

    log("")
    log("=" * 70)
    log("DONE")
    log("=" * 70)


if __name__ == "__main__":
    main()
