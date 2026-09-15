"""
enrich_fundamentals.py  —  STEP 3 of the pipeline

This is the part that changes the economics of the whole project.

The obvious way to get fundamentals from EDGAR is the companyfacts endpoint:
one request per company, each response several megabytes. For 5,000 companies
that is 5,000 requests and multiple gigabytes, every refresh. It works, but it
is exactly the access pattern that gets an IP throttled.

The XBRL *frames* API inverts the query. Instead of "all concepts for one
company" it returns "one concept for every company that reported it in a given
period":

    https://data.sec.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2024.json

So the entire market's revenue for a year is a single request. The whole
fundamentals layer below costs a couple of hundred requests rather than
several thousand, and most of those are served from cache on later runs.

Two things to know about frames data:
  * Companies with off-calendar fiscal years get slotted into the calendar
    frame they best fit, so we pull several periods and keep the most recent
    value per company by its actual period-end date.
  * Revenue has no single canonical tag. Post-ASC-606 filers mostly use
    RevenueFromContractWithCustomer*, older ones use Revenues or
    SalesRevenueNet. We query all of them and coalesce by priority.

Output: data/fundamentals.csv
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import sys
from collections import defaultdict

from common import DATA_DIR, BudgetExhausted, make_sec_client, setup_logging

log = setup_logging("fundamentals")

FRAMES_URL = "https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"

ANNUAL_YEARS = int(os.environ.get("ANNUAL_YEARS", "4"))
INSTANT_QUARTERS = int(os.environ.get("INSTANT_QUARTERS", "6"))
QUARTERLY_PERIODS = int(os.environ.get("QUARTERLY_PERIODS", "6"))

# Recent frames still change as filings arrive; old ones essentially never do.
FRESH_TTL = 5 * 86_400
STALE_TTL = 60 * 86_400
FRESH_PERIOD_COUNT = 2


# (output field, taxonomy, tag, unit, priority)  — lower priority wins
ANNUAL_CONCEPTS = [
    ("Revenue", "us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD", 1),
    ("Revenue", "us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax", "USD", 2),
    ("Revenue", "us-gaap", "Revenues", "USD", 3),
    ("Revenue", "us-gaap", "SalesRevenueNet", "USD", 4),
    ("NetIncome", "us-gaap", "NetIncomeLoss", "USD", 1),
    ("NetIncome", "us-gaap", "ProfitLoss", "USD", 2),
    ("GrossProfit", "us-gaap", "GrossProfit", "USD", 1),
    ("OperatingIncome", "us-gaap", "OperatingIncomeLoss", "USD", 1),
    ("RnD", "us-gaap", "ResearchAndDevelopmentExpense", "USD", 1),
    ("SBC", "us-gaap", "ShareBasedCompensation", "USD", 1),
    ("InterestExpense", "us-gaap", "InterestExpense", "USD", 1),
    ("OCF", "us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD", 1),
    ("OCF", "us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", "USD", 2),
    ("CapEx", "us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment", "USD", 1),
    ("Dividends", "us-gaap", "PaymentsOfDividendsCommonStock", "USD", 1),
    ("Buybacks", "us-gaap", "PaymentsForRepurchaseOfCommonStock", "USD", 1),
    ("DilutedShares", "us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", 1),
    ("EPSDiluted", "us-gaap", "EarningsPerShareDiluted", "USD-per-shares", 1),
]

INSTANT_CONCEPTS = [
    ("Assets", "us-gaap", "Assets", "USD", 1),
    ("Liabilities", "us-gaap", "Liabilities", "USD", 1),
    ("Equity", "us-gaap", "StockholdersEquity", "USD", 1),
    ("Equity", "us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD", 2),
    ("AssetsCurrent", "us-gaap", "AssetsCurrent", "USD", 1),
    ("LiabilitiesCurrent", "us-gaap", "LiabilitiesCurrent", "USD", 1),
    ("Cash", "us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD", 1),
    ("ShortTermInvestments", "us-gaap", "ShortTermInvestments", "USD", 1),
    ("LongTermDebt", "us-gaap", "LongTermDebt", "USD", 1),
    ("LongTermDebt", "us-gaap", "LongTermDebtNoncurrent", "USD", 2),
    ("CurrentDebt", "us-gaap", "LongTermDebtCurrent", "USD", 1),
    ("Goodwill", "us-gaap", "Goodwill", "USD", 1),
    ("Inventory", "us-gaap", "InventoryNet", "USD", 1),
    ("RetainedEarnings", "us-gaap", "RetainedEarningsAccumulatedDeficit", "USD", 1),
    ("SharesOutstanding", "dei", "EntityCommonStockSharesOutstanding", "shares", 1),
    ("SharesOutstanding", "us-gaap", "CommonStockSharesOutstanding", "shares", 2),
    ("PublicFloat", "dei", "EntityPublicFloat", "USD", 1),
]

QUARTERLY_CONCEPTS = [
    ("QRevenue", "us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD", 1),
    ("QRevenue", "us-gaap", "Revenues", "USD", 2),
    ("QNetIncome", "us-gaap", "NetIncomeLoss", "USD", 1),
]

# How many historical values to keep per annual field.
ANNUAL_DEPTH = 3
QUARTERLY_DEPTH = 5


def annual_periods(today: dt.date) -> list[str]:
    return [f"CY{today.year - i}" for i in range(ANNUAL_YEARS)]


def quarter_sequence(today: dt.date, count: int) -> list[tuple[int, int]]:
    year, quarter = today.year, (today.month - 1) // 3 + 1
    out = []
    for _ in range(count):
        out.append((year, quarter))
        quarter -= 1
        if quarter == 0:
            quarter, year = 4, year - 1
    return out


def instant_periods(today: dt.date) -> list[str]:
    return [f"CY{y}Q{q}I" for y, q in quarter_sequence(today, INSTANT_QUARTERS)]


def duration_quarter_periods(today: dt.date) -> list[str]:
    return [f"CY{y}Q{q}" for y, q in quarter_sequence(today, QUARTERLY_PERIODS)]


def fetch_frames(client, concepts, periods, store, label):
    """
    store[cik][field][end] = (priority, value, period)

    Keying on the reported period-end date rather than the calendar frame is
    what makes off-calendar fiscal years line up correctly.
    """
    total = len(concepts) * len(periods)
    done = hits = 0

    for period_index, period in enumerate(periods):
        ttl = FRESH_TTL if period_index < FRESH_PERIOD_COUNT else STALE_TTL
        for field, taxonomy, tag, unit, priority in concepts:
            url = FRAMES_URL.format(taxonomy=taxonomy, tag=tag, unit=unit, period=period)
            done += 1
            try:
                data = client.get_json(url, cache_ttl=ttl, allow_404=True)
            except BudgetExhausted:
                log.error("Request budget hit during %s frames; using what we have.", label)
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("frame %s/%s %s failed: %s", tag, unit, period, exc)
                continue

            if not data or "data" not in data:
                continue

            rows = data["data"]
            hits += len(rows)
            for row in rows:
                cik = row.get("cik")
                val = row.get("val")
                end = row.get("end") or period
                if cik is None or val is None:
                    continue
                bucket = store[cik][field]
                existing = bucket.get(end)
                if existing is None or priority < existing[0]:
                    bucket[end] = (priority, val, period)

            if done % 25 == 0:
                log.info("  %s frames %d/%d — %d facts so far", label, done, total, hits)

    log.info("%s frames complete: %d requests planned, %d facts collected", label, total, hits)


def flatten(store, annual_fields, instant_fields, quarterly_fields) -> list[dict]:
    rows = []
    for cik, fields in store.items():
        row: dict = {"CIK": cik}

        for field in annual_fields:
            series = sorted(fields.get(field, {}).items(), reverse=True)
            for i in range(ANNUAL_DEPTH):
                suffix = "" if i == 0 else f" FY-{i}"
                if i < len(series):
                    end, (_, val, _) = series[i]
                    row[f"{field}{suffix}"] = val
                    if i == 0:
                        row[f"{field} Period End"] = end
                else:
                    row[f"{field}{suffix}"] = ""
                    if i == 0:
                        row.setdefault(f"{field} Period End", "")

        for field in instant_fields:
            series = sorted(fields.get(field, {}).items(), reverse=True)
            if series:
                end, (_, val, _) = series[0]
                row[field] = val
                row[f"{field} As Of"] = end
            else:
                row[field] = ""
                row[f"{field} As Of"] = ""

        for field in quarterly_fields:
            series = sorted(fields.get(field, {}).items(), reverse=True)
            for i in range(QUARTERLY_DEPTH):
                suffix = "" if i == 0 else f" Q-{i}"
                row[f"{field}{suffix}"] = series[i][1][1] if i < len(series) else ""
            row[f"{field} Period End"] = series[0][0] if series else ""

        rows.append(row)
    return rows


def main() -> int:
    today = dt.date.today()
    client = make_sec_client()

    store: dict = defaultdict(lambda: defaultdict(dict))

    log.info("Pulling annual frames for %s", ", ".join(annual_periods(today)))
    fetch_frames(client, ANNUAL_CONCEPTS, annual_periods(today), store, "annual")

    log.info("Pulling instant frames for %s", ", ".join(instant_periods(today)))
    fetch_frames(client, INSTANT_CONCEPTS, instant_periods(today), store, "instant")

    log.info("Pulling quarterly frames for %s", ", ".join(duration_quarter_periods(today)))
    fetch_frames(client, QUARTERLY_CONCEPTS, duration_quarter_periods(today), store, "quarterly")

    annual_fields = sorted({c[0] for c in ANNUAL_CONCEPTS})
    instant_fields = sorted({c[0] for c in INSTANT_CONCEPTS})
    quarterly_fields = sorted({c[0] for c in QUARTERLY_CONCEPTS})

    rows = flatten(store, annual_fields, instant_fields, quarterly_fields)
    if not rows:
        log.error("No fundamentals collected — leaving any existing file alone.")
        return 1

    fieldnames = ["CIK"]
    for f in annual_fields:
        fieldnames += [f] + [f"{f} FY-{i}" for i in range(1, ANNUAL_DEPTH)] + [f"{f} Period End"]
    for f in instant_fields:
        fieldnames += [f, f"{f} As Of"]
    for f in quarterly_fields:
        fieldnames += [f] + [f"{f} Q-{i}" for i in range(1, QUARTERLY_DEPTH)] + [f"{f} Period End"]

    out = DATA_DIR / "fundamentals.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=lambda r: r["CIK"]):
            writer.writerow({k: row.get(k, "") for k in fieldnames})

    log.info("Wrote %s — %d filers with at least one fact", out, len(rows))
    log.info(client.stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())
