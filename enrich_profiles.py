"""
enrich_profiles.py  —  STEP 2 of the pipeline

One request per company against the SEC submissions API, which returns both
the company's identity metadata and its recent filing history.

The filing history is the part most screeners throw away, and it is the part
that tells you the most about what a company has actually been *doing*:

  * Is the latest 10-K stale? (annual report older than ~15 months)
  * Have they filed NT 10-K / NT 10-Q? (a "we can't file on time" notice —
    one of the cleanest public red flags there is)
  * How many 424B prospectuses in the last year? (each one is usually a
    capital raise, i.e. dilution)
  * How many Form 4s? (insider transaction volume)
  * How many 8-Ks? (event/news flow)
  * How long has the company been an SEC filer at all?

None of this costs an extra request. It is already in the response.

Cost: ~1 request per company, cached for PROFILE_TTL_DAYS.
Output: data/profiles.csv
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import sys
from collections import Counter

from common import (
    DATA_DIR,
    BudgetExhausted,
    make_sec_client,
    setup_logging,
    sic_to_sector,
)

log = setup_logging("profiles")

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

PROFILE_TTL_DAYS = float(os.environ.get("PROFILE_TTL_DAYS", "7"))
MAX_PROFILE_FETCHES = int(os.environ.get("MAX_PROFILE_FETCHES", "12000"))

# Only bother with security types that represent an actual equity stake.
EQUITY_TYPES = {"Common Stock", "Ordinary Shares", "ADR"}

ANNUAL_FORMS = {"10-K", "10-K/A", "10-KSB", "20-F", "20-F/A", "40-F", "40-F/A"}
QUARTERLY_FORMS = {"10-Q", "10-Q/A"}
LATE_FORMS = {"NT 10-K", "NT 10-Q", "NT 20-F", "NT 10-K/A", "NT 10-Q/A"}

FIELDNAMES = [
    "CIK",
    "SEC Company Name",
    "SIC Code",
    "Industry (SIC Description)",
    "Sector",
    "Entity Type",
    "Filer Category",
    "State of Incorporation",
    "Business City",
    "Business State",
    "Business Country",
    "Phone",
    "Fiscal Year End",
    "SEC Exchanges",
    "SEC Tickers",
    "Former Names",
    "First EDGAR Filing",
    "Latest Filing Date",
    "Latest Annual Report",
    "Latest Annual Form",
    "Latest Quarterly Report",
    "Months Since Annual Report",
    "Filings Last 12M",
    "8-K Last 12M",
    "Form 4 Last 12M",
    "424B Last 12M",
    "S-1/S-3 Last 12M",
    "SC 13D/G Last 12M",
    "Late Filing Notice 24M",
    "Insider Filings Exist",
    "Profile Fetched",
]


def _months_between(earlier: str, later: dt.date) -> str:
    try:
        d = dt.date.fromisoformat(earlier)
    except (ValueError, TypeError):
        return ""
    return str(round((later - d).days / 30.44, 1))


def parse_submissions(cik: int, data: dict, today: dt.date) -> dict:
    addr = (data.get("addresses") or {}).get("business") or {}
    former = [n.get("name") for n in data.get("formerNames", []) if n.get("name")]

    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    pairs = list(zip(forms, dates))

    cutoff_12m = (today - dt.timedelta(days=365)).isoformat()
    cutoff_24m = (today - dt.timedelta(days=730)).isoformat()

    counts = Counter()
    latest_annual = latest_annual_form = latest_quarterly = ""
    late_notice = ""

    for form, date in pairs:
        form = (form or "").strip()
        date = (date or "").strip()
        if not form or not date:
            continue

        if form in ANNUAL_FORMS and date > latest_annual:
            latest_annual, latest_annual_form = date, form
        if form in QUARTERLY_FORMS and date > latest_quarterly:
            latest_quarterly = date
        if form in LATE_FORMS and date >= cutoff_24m:
            late_notice = "Y"

        if date >= cutoff_12m:
            counts["total"] += 1
            if form.startswith("8-K"):
                counts["8k"] += 1
            elif form in ("4", "4/A"):
                counts["form4"] += 1
            elif form.startswith("424B"):
                counts["424b"] += 1
            elif form.startswith("S-1") or form.startswith("S-3"):
                counts["s1s3"] += 1
            elif form.startswith("SC 13D") or form.startswith("SC 13G"):
                counts["13dg"] += 1

    # The submissions API pages older history into separate files. Their
    # metadata alone gives us the earliest filing date, so we can report
    # "SEC filer since" without downloading anything extra.
    first_filing = ""
    for f in (data.get("filings") or {}).get("files", []):
        got = (f.get("filingFrom") or "").strip()
        if got and (not first_filing or got < first_filing):
            first_filing = got
    if not first_filing and dates:
        first_filing = min(d for d in dates if d)

    latest_filing = max((d for d in dates if d), default="")
    sic = str(data.get("sic") or "")

    return {
        "CIK": cik,
        "SEC Company Name": data.get("name", ""),
        "SIC Code": sic,
        "Industry (SIC Description)": data.get("sicDescription", ""),
        "Sector": sic_to_sector(sic),
        "Entity Type": data.get("entityType", ""),
        "Filer Category": data.get("category", ""),
        "State of Incorporation": data.get("stateOfIncorporation", ""),
        "Business City": addr.get("city", "") or "",
        "Business State": addr.get("stateOrCountry", "") or "",
        "Business Country": addr.get("stateOrCountryDescription", "") or "",
        "Phone": data.get("phone", "") or "",
        "Fiscal Year End": data.get("fiscalYearEnd", "") or "",
        "SEC Exchanges": ", ".join(data.get("exchanges") or []),
        "SEC Tickers": ", ".join(data.get("tickers") or []),
        "Former Names": "; ".join(former),
        "First EDGAR Filing": first_filing,
        "Latest Filing Date": latest_filing,
        "Latest Annual Report": latest_annual,
        "Latest Annual Form": latest_annual_form,
        "Latest Quarterly Report": latest_quarterly,
        "Months Since Annual Report": _months_between(latest_annual, today),
        "Filings Last 12M": counts["total"],
        "8-K Last 12M": counts["8k"],
        "Form 4 Last 12M": counts["form4"],
        "424B Last 12M": counts["424b"],
        "S-1/S-3 Last 12M": counts["s1s3"],
        "SC 13D/G Last 12M": counts["13dg"],
        "Late Filing Notice 24M": late_notice,
        "Insider Filings Exist": "Y" if data.get("insiderTransactionForIssuerExists") else "",
        "Profile Fetched": today.isoformat(),
    }


def load_targets() -> list[int]:
    path = DATA_DIR / "universe.csv"
    if not path.exists():
        raise SystemExit("data/universe.csv missing — run build_universe.py first.")

    ciks: dict[int, None] = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["Security Type"] not in EQUITY_TYPES:
                continue
            if not row["CIK"]:
                continue
            ciks[int(row["CIK"])] = None
    return list(ciks)


def load_previous() -> dict[int, dict]:
    path = DATA_DIR / "profiles.csv"
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                out[int(row["CIK"])] = row
            except (KeyError, ValueError):
                continue
    return out


def main() -> int:
    today = dt.date.today()
    client = make_sec_client()
    targets = load_targets()
    previous = load_previous()

    # Companies we have never seen go first, then the stalest. If the run is
    # cut short we still make forward progress every week instead of
    # re-fetching the same alphabetical prefix.
    targets.sort(
        key=lambda c: (
            c in previous,
            previous.get(c, {}).get("Profile Fetched", ""),
        )
    )

    log.info(
        "%d companies to profile (%d already cached from a previous run)",
        len(targets),
        len(previous),
    )

    ttl = PROFILE_TTL_DAYS * 86_400
    results: dict[int, dict] = {}
    fetched = failed = reused = 0

    for i, cik in enumerate(targets, 1):
        if fetched >= MAX_PROFILE_FETCHES:
            if cik in previous:
                results[cik] = previous[cik]
                reused += 1
            continue

        try:
            data = client.get_json(SUBMISSIONS_URL.format(cik=cik), cache_ttl=ttl, allow_404=True)
        except BudgetExhausted as exc:
            log.error("Stopping profile fetch early: %s", exc)
            break
        except Exception as exc:  # noqa: BLE001 - one bad company must not kill the run
            log.warning("CIK %s failed: %s", cik, exc)
            failed += 1
            if cik in previous:
                results[cik] = previous[cik]
            continue

        if not data:
            failed += 1
            if cik in previous:
                results[cik] = previous[cik]
            continue

        results[cik] = parse_submissions(cik, data, today)
        fetched += 1

        if i % 500 == 0:
            log.info("  %d/%d  (%s)", i, len(targets), client.stats())

    # Anything we never got to keeps its previous row rather than vanishing.
    for cik, row in previous.items():
        results.setdefault(cik, row)

    out = DATA_DIR / "profiles.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for cik in sorted(results):
            writer.writerow({k: results[cik].get(k, "") for k in FIELDNAMES})

    log.info(
        "Wrote %s — %d profiles (%d refreshed, %d reused, %d failed)",
        out, len(results), fetched, reused, failed,
    )
    log.info(client.stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())
