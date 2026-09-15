"""
build_dataset.py  —  FINAL STEP of the pipeline

Joins the three SEC-derived layers, computes the ratios you would otherwise
compute by hand, and writes the browsable output.

No network access at all. This step is pure local computation, so you can
re-run it as often as you like while you tune what you want to see.

A note on valuation
-------------------
There is no price data in this dataset, by design — every number here comes
from SEC filings, which means it is authoritative and will not silently rot
when some free third-party endpoint changes.

The cost of that is you cannot compute a live P/E. Two things substitute:

  * Per-share columns. Revenue, book value, free cash flow and net cash are
    all expressed per share, so once you look up a price for a company you
    actually care about, the multiple is one division away.
  * Public float, from the 10-K cover page. It is a real dollar figure filed
    with the SEC, but it is measured on one specific date — usually the last
    business day of the company's most recent second quarter — so it can be
    up to a year stale, and it excludes insider-held shares. Treat the
    Float/... ratios as a rough sort order for browsing, never as a valuation.

Outputs
-------
us_listed_companies.csv   full dataset, one row per listed equity
us_listed_companies.xlsx  same data with frozen headers, autofilters,
                          sensible number formats and a data-dictionary tab
data/changes.csv          tickers added or removed since the last run
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from common import DATA_DIR, ROOT, setup_logging

log = setup_logging("dataset")

EQUITY_TYPES = {"Common Stock", "Ordinary Shares", "ADR"}
MILLION = 1_000_000.0


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def read(name: str, **kwargs) -> pd.DataFrame:
    path = DATA_DIR / name
    if not path.exists():
        log.warning("%s not found — continuing without it", path)
        return pd.DataFrame()
    return pd.read_csv(path, **kwargs)


def load() -> pd.DataFrame:
    universe = read("universe.csv", dtype={"CIK": "string"})
    if universe.empty:
        raise SystemExit("data/universe.csv is missing — run build_universe.py first.")

    universe = universe[universe["Security Type"].isin(EQUITY_TYPES)].copy()
    universe = universe[universe["CIK"].notna() & (universe["CIK"] != "")]
    universe["CIK"] = universe["CIK"].astype(float).astype("Int64")

    df = universe
    for name in ("profiles.csv", "fundamentals.csv"):
        part = read(name)
        if part.empty:
            continue
        part["CIK"] = pd.to_numeric(part["CIK"], errors="coerce").astype("Int64")
        part = part.drop_duplicates(subset=["CIK"])
        df = df.merge(part, on="CIK", how="left", suffixes=("", f"_{name[:4]}"))

    log.info("Joined dataset: %d rows x %d columns", len(df), len(df.columns))
    return df


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------

def num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce")


def text(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series("", index=df.index, dtype="object")
    return df[col].fillna("").astype(str).str.strip()


def ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return numerator / denominator.replace(0, np.nan)


def pct(series: pd.Series, digits: int = 2) -> pd.Series:
    return (series * 100).round(digits)


def derive(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    revenue = num(out, "Revenue")
    revenue_1 = num(out, "Revenue FY-1")
    revenue_2 = num(out, "Revenue FY-2")
    net_income = num(out, "NetIncome")
    net_income_1 = num(out, "NetIncome FY-1")
    gross_profit = num(out, "GrossProfit")
    op_income = num(out, "OperatingIncome")
    ocf = num(out, "OCF")
    capex = num(out, "CapEx").abs()
    rnd = num(out, "RnD")
    sbc = num(out, "SBC")
    interest = num(out, "InterestExpense").abs()
    dividends = num(out, "Dividends").abs()
    buybacks = num(out, "Buybacks").abs()

    assets = num(out, "Assets")
    equity = num(out, "Equity")
    cash = num(out, "Cash").fillna(0)
    sti = num(out, "ShortTermInvestments").fillna(0)
    ltd = num(out, "LongTermDebt").fillna(0)
    cud = num(out, "CurrentDebt").fillna(0)
    assets_cur = num(out, "AssetsCurrent")
    liab_cur = num(out, "LiabilitiesCurrent")
    retained = num(out, "RetainedEarnings")

    shares = num(out, "SharesOutstanding")
    diluted = num(out, "DilutedShares")
    diluted_1 = num(out, "DilutedShares FY-1")
    public_float = num(out, "PublicFloat")

    fcf = ocf - capex
    total_debt = ltd + cud
    liquid = cash + sti
    net_cash = liquid - total_debt

    # --- size ---
    out["Public Float ($M)"] = (public_float / MILLION).round(1)
    out["Shares Outstanding (M)"] = (shares / MILLION).round(2)

    # --- absolute figures, in millions for readability ---
    out["Revenue ($M)"] = (revenue / MILLION).round(1)
    out["Revenue FY-1 ($M)"] = (revenue_1 / MILLION).round(1)
    out["Revenue FY-2 ($M)"] = (revenue_2 / MILLION).round(1)
    out["Gross Profit ($M)"] = (gross_profit / MILLION).round(1)
    out["Operating Income ($M)"] = (op_income / MILLION).round(1)
    out["Net Income ($M)"] = (net_income / MILLION).round(1)
    out["Operating Cash Flow ($M)"] = (ocf / MILLION).round(1)
    out["CapEx ($M)"] = (capex / MILLION).round(1)
    out["Free Cash Flow ($M)"] = (fcf / MILLION).round(1)
    out["R&D ($M)"] = (rnd / MILLION).round(1)
    out["Stock Comp ($M)"] = (sbc / MILLION).round(1)
    out["Total Assets ($M)"] = (assets / MILLION).round(1)
    out["Shareholders Equity ($M)"] = (equity / MILLION).round(1)
    out["Cash & Investments ($M)"] = (liquid / MILLION).round(1)
    out["Total Debt ($M)"] = (total_debt / MILLION).round(1)
    out["Net Cash ($M)"] = (net_cash / MILLION).round(1)
    out["Retained Earnings ($M)"] = (retained / MILLION).round(1)

    # --- per share: the bridge to any price you look up yourself ---
    out["Revenue per Share"] = ratio(revenue, shares).round(2)
    out["Book Value per Share"] = ratio(equity, shares).round(2)
    out["FCF per Share"] = ratio(fcf, shares).round(2)
    out["Net Cash per Share"] = ratio(net_cash, shares).round(2)

    # --- margins and returns ---
    out["Gross Margin %"] = pct(ratio(gross_profit, revenue))
    out["Operating Margin %"] = pct(ratio(op_income, revenue))
    out["Net Margin %"] = pct(ratio(net_income, revenue))
    out["FCF Margin %"] = pct(ratio(fcf, revenue))
    out["R&D % of Revenue"] = pct(ratio(rnd, revenue))
    out["Stock Comp % of Revenue"] = pct(ratio(sbc, revenue))
    out["Return on Equity %"] = pct(ratio(net_income, equity))
    out["Return on Assets %"] = pct(ratio(net_income, assets))
    out["Return on Capital %"] = pct(ratio(op_income, (equity + total_debt)))

    # --- growth ---
    out["Revenue Growth 1Y %"] = pct(ratio(revenue, revenue_1) - 1)
    cagr2 = ratio(revenue, revenue_2)
    out["Revenue CAGR 2Y %"] = pct(np.sqrt(cagr2.where(cagr2 > 0)) - 1)
    out["Net Income Growth 1Y %"] = pct(
        ratio(net_income - net_income_1, net_income_1.abs())
    )
    out["Share Count Change 1Y %"] = pct(ratio(diluted, diluted_1) - 1)

    q_rev = num(out, "QRevenue")
    q_rev_4 = num(out, "QRevenue Q-4")
    out["Latest Qtr Revenue ($M)"] = (q_rev / MILLION).round(1)
    out["Qtr Revenue YoY %"] = pct(ratio(q_rev, q_rev_4) - 1)

    # --- balance sheet health ---
    out["Current Ratio"] = ratio(assets_cur, liab_cur).round(2)
    out["Debt / Equity"] = ratio(total_debt, equity).round(2)
    out["Net Debt / FCF"] = ratio(-net_cash, fcf).round(2)
    out["Interest Coverage"] = ratio(op_income, interest).round(2)
    out["Cash % of Assets"] = pct(ratio(liquid, assets))

    # --- rough valuation anchors, float-based. See module docstring. ---
    out["Float / Revenue"] = ratio(public_float, revenue).round(2)
    out["Float / Net Income"] = ratio(public_float, net_income.where(net_income > 0)).round(2)
    out["Float / Book Value"] = ratio(public_float, equity.where(equity > 0)).round(2)
    out["Float / FCF"] = ratio(public_float, fcf.where(fcf > 0)).round(2)

    out["Shareholder Return ($M)"] = ((dividends + buybacks) / MILLION).round(1)
    out["Rule of 40"] = (
        out["Revenue Growth 1Y %"].fillna(0) + out["FCF Margin %"].fillna(0)
    ).round(1)

    # --- simple handles for filtering in Excel ---
    out["Profitable"] = np.where(net_income > 0, "Y", np.where(net_income.notna(), "N", ""))
    out["FCF Positive"] = np.where(fcf > 0, "Y", np.where(fcf.notna(), "N", ""))
    out["Growing"] = np.where(
        out["Revenue Growth 1Y %"] > 0, "Y",
        np.where(out["Revenue Growth 1Y %"].notna(), "N", ""),
    )
    out["Net Cash Positive"] = np.where(
        net_cash > 0, "Y", np.where(net_cash.notna(), "N", "")
    )

    out = add_flags(out, equity, retained)
    out["Data Completeness %"] = completeness(out)
    return out


def add_flags(out: pd.DataFrame, equity, retained) -> pd.DataFrame:
    """
    A single human-readable column of things worth a second look.

    These are observations, not judgements. A clinical-stage biotech will trip
    several of them by design; a stale filer tripping all of them is a
    different story. The point is to make the odd ones easy to notice when you
    are scrolling, not to score anything.
    """
    months_stale = num(out, "Months Since Annual Report")
    dilution = num(out, "Share Count Change 1Y %")
    raises = num(out, "424B Last 12M")

    compliance_codes = {
        "Deficient", "Delinquent", "Bankrupt", "Deficient & Bankrupt",
        "Deficient & Delinquent", "Delinquent & Bankrupt",
        "Deficient, Delinquent & Bankrupt",
    }

    conditions = [
        (text(out, "Late Filing Notice 24M") == "Y", "late-filing notice"),
        (text(out, "Financial Status").isin(compliance_codes), "exchange compliance issue"),
        (months_stale > 15, "annual report >15 months old"),
        (equity < 0, "negative book equity"),
        (retained < 0, "accumulated deficit"),
        (dilution > 20, "share count +20% in 1y"),
        (raises >= 4, "4+ prospectuses in 12m"),
        (text(out, "Likely SPAC") == "Y", "likely SPAC/shell"),
    ]

    parts = []
    count = pd.Series(0, index=out.index, dtype="int64")
    for mask, label in conditions:
        mask = pd.Series(mask, index=out.index).fillna(False).astype(bool)
        parts.append(mask.map({True: label, False: ""}))
        count += mask.astype(int)

    flags = parts[0]
    for part in parts[1:]:
        joined = flags.str.cat(part, sep="; ")
        flags = pd.Series(
            np.where(
                (flags != "") & (part != ""), joined,
                np.where(flags != "", flags, part),
            ),
            index=out.index,
        )

    out["Flags"] = flags
    out["Flag Count"] = count
    return out


KEY_FIELDS = [
    "Revenue ($M)", "Net Income ($M)", "Total Assets ($M)",
    "Shareholders Equity ($M)", "Operating Cash Flow ($M)",
    "Shares Outstanding (M)", "Industry (SIC Description)",
]


def completeness(out: pd.DataFrame) -> pd.Series:
    present = pd.Series(0, index=out.index, dtype="float64")
    available = 0
    for field in KEY_FIELDS:
        if field not in out.columns:
            continue
        available += 1
        present += out[field].notna() & (out[field].astype(str).str.strip() != "")
    if available == 0:
        return present
    return (present / available * 100).round(0)


# ---------------------------------------------------------------------------
# Column order
# ---------------------------------------------------------------------------

COLUMN_ORDER = [
    # identity
    "Ticker", "Issuer Name", "SEC Company Name", "Exchange", "Market Tier",
    "Sector", "Industry (SIC Description)", "SIC Code", "CIK",
    "Business City", "Business State", "Business Country",
    "State of Incorporation", "Filer Category", "Entity Type",
    # size
    "Public Float ($M)", "PublicFloat As Of", "Shares Outstanding (M)",
    "Revenue ($M)", "Total Assets ($M)",
    # rough valuation anchors
    "Float / Revenue", "Float / Net Income", "Float / Book Value", "Float / FCF",
    # per share
    "Revenue per Share", "Book Value per Share", "FCF per Share",
    "Net Cash per Share", "EPSDiluted",
    # income statement
    "Revenue FY-1 ($M)", "Revenue FY-2 ($M)", "Gross Profit ($M)",
    "Operating Income ($M)", "Net Income ($M)", "R&D ($M)", "Stock Comp ($M)",
    # margins and returns
    "Gross Margin %", "Operating Margin %", "Net Margin %", "FCF Margin %",
    "Return on Equity %", "Return on Assets %", "Return on Capital %",
    "R&D % of Revenue", "Stock Comp % of Revenue",
    # growth
    "Revenue Growth 1Y %", "Revenue CAGR 2Y %", "Net Income Growth 1Y %",
    "Latest Qtr Revenue ($M)", "Qtr Revenue YoY %", "Rule of 40",
    "Share Count Change 1Y %",
    # cash flow and balance sheet
    "Operating Cash Flow ($M)", "CapEx ($M)", "Free Cash Flow ($M)",
    "Shareholders Equity ($M)", "Cash & Investments ($M)", "Total Debt ($M)",
    "Net Cash ($M)", "Retained Earnings ($M)", "Shareholder Return ($M)",
    "Current Ratio", "Debt / Equity", "Net Debt / FCF", "Interest Coverage",
    "Cash % of Assets",
    # filing behaviour
    "Latest Annual Report", "Latest Annual Form", "Months Since Annual Report",
    "Latest Quarterly Report", "Latest Filing Date", "First EDGAR Filing",
    "Filings Last 12M", "8-K Last 12M", "Form 4 Last 12M", "424B Last 12M",
    "S-1/S-3 Last 12M", "SC 13D/G Last 12M", "Late Filing Notice 24M",
    "Financial Status",
    # handles and provenance
    "Profitable", "FCF Positive", "Growing", "Net Cash Positive",
    "Flags", "Flag Count", "Data Completeness %",
    "Fiscal Year End", "Revenue Period End", "Assets As Of",
    "Former Names", "SEC Exchanges", "Security Name",
]


def order_columns(df: pd.DataFrame) -> pd.DataFrame:
    present = [c for c in COLUMN_ORDER if c in df.columns]
    rest = [c for c in df.columns if c not in present]
    return df[present + rest]


# ---------------------------------------------------------------------------
# Excel output
# ---------------------------------------------------------------------------

DICTIONARY = [
    ("Public Float ($M)", "Market value of shares held by non-affiliates, from the 10-K cover page. Filed with the SEC, but measured on one date — check 'PublicFloat As Of'. It can be up to a year stale and excludes insider-held shares."),
    ("Float / Revenue", "Public float divided by annual revenue. A rough sort order for browsing, not a valuation — the float date and the fiscal year end are usually months apart."),
    ("Revenue per Share", "Annual revenue / shares outstanding. Divide any price you look up by this to get a price-to-sales ratio."),
    ("Book Value per Share", "Shareholders equity / shares outstanding. Compare against a price for price-to-book."),
    ("Net Cash per Share", "(Cash + short-term investments − total debt) / shares. When this is a large fraction of the share price, the operating business is being valued cheaply."),
    ("Return on Capital %", "Operating income / (equity + total debt). Less distorted by leverage than return on equity."),
    ("Rule of 40", "Revenue growth % + FCF margin %. A software-industry rule of thumb."),
    ("Share Count Change 1Y %", "Change in weighted-average diluted shares. Positive means dilution."),
    ("424B Last 12M", "Prospectus filings in the last year. Usually indicates capital raises."),
    ("Form 4 Last 12M", "Insider transaction filings. Counts activity; does not distinguish buys from sells."),
    ("Late Filing Notice 24M", "Company filed NT 10-K or NT 10-Q, i.e. missed a reporting deadline."),
    ("Financial Status", "Nasdaq listing-compliance code. Deficient/Delinquent/Bankrupt are warnings."),
    ("Months Since Annual Report", "Age of the most recent 10-K/20-F/40-F. Over ~15 usually means a problem."),
    ("Retained Earnings ($M)", "Negative means an accumulated deficit — the company has lost money in total to date."),
    ("Flags", "Plain-language observations worth checking. Not a verdict."),
    ("Data Completeness %", "Share of key fields populated. Low values usually mean a small or foreign filer."),
    ("Revenue Period End", "Actual fiscal period end behind the annual figures, not the calendar frame."),
]

NOTES = [
    "Every figure here comes from SEC filings. Nothing is a recommendation, and none of it is adjusted for restatements, share-class overlap or one-off items.",
    "There is no price data in this dataset. Use the per-share columns with a price you look up to get any multiple you want.",
    "Annual figures are the most recent fiscal year each company reported, so different rows can cover different periods. Check 'Revenue Period End' before comparing two companies.",
    "Financial-sector filers (banks, insurers, REITs) often leave Revenue and Gross Profit blank because their income statements use different XBRL tags. Blank does not mean zero.",
    "Public float is filed on the 10-K cover page as of a single date, typically the last business day of the company's second quarter. The Float/... ratios inherit that staleness.",
]


def write_excel(df: pd.DataFrame, path: Path) -> None:
    try:
        import xlsxwriter  # noqa: F401
    except ImportError:
        log.warning("xlsxwriter not installed — writing plain Excel via openpyxl")
        df.to_excel(path, index=False)
        return

    money = [c for c in df.columns if c.endswith("($M)")]
    percent = [c for c in df.columns if c.endswith("%")]
    wide = {
        "Issuer Name", "SEC Company Name", "Industry (SIC Description)",
        "Flags", "Security Name", "Former Names",
    }

    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        df.to_excel(writer, sheet_name="Universe", index=False)
        book = writer.book
        sheet = writer.sheets["Universe"]

        header = book.add_format(
            {"bold": True, "bg_color": "#1F3864", "font_color": "white",
             "border": 1, "text_wrap": True, "valign": "vcenter"}
        )
        fmt_money = book.add_format({"num_format": "#,##0.0"})
        fmt_pct = book.add_format({"num_format": "0.0"})

        for idx, name in enumerate(df.columns):
            sheet.write(0, idx, name, header)
            if name in money:
                width, fmt = 14, fmt_money
            elif name in percent:
                width, fmt = 12, fmt_pct
            elif name in wide:
                width, fmt = 34, None
            else:
                width, fmt = 13, None
            sheet.set_column(idx, idx, width, fmt)

        sheet.freeze_panes(1, 2)
        sheet.autofilter(0, 0, len(df), len(df.columns) - 1)
        sheet.set_row(0, 34)

        meta = book.add_worksheet("Read Me")
        bold = book.add_format({"bold": True})
        wrap = book.add_format({"text_wrap": True, "valign": "top"})
        meta.set_column(0, 0, 32)
        meta.set_column(1, 1, 95, wrap)
        meta.write(0, 0, "Built", bold)
        meta.write(0, 1, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        meta.write(1, 0, "Rows", bold)
        meta.write(1, 1, len(df))
        meta.write(2, 0, "Source", bold)
        meta.write(2, 1, "SEC EDGAR and Nasdaq Trader. No price data.")
        row = 4
        meta.write(row, 0, "How to read this", bold)
        row += 1
        for note in NOTES:
            meta.write(row, 1, note, wrap)
            row += 1
        row += 1
        meta.write(row, 0, "Column", bold)
        meta.write(row, 1, "Meaning", bold)
        row += 1
        for name, desc in DICTIONARY:
            meta.write(row, 0, name)
            meta.write(row, 1, desc, wrap)
            row += 1


# ---------------------------------------------------------------------------
# Change log
# ---------------------------------------------------------------------------

def write_changes(df: pd.DataFrame, previous_path: Path) -> None:
    """
    Diff this run against the committed copy from last week.

    Newly appearing tickers are IPOs, direct listings, spin-offs and uplistings
    from OTC — which is exactly the pond you want to be looking in for names
    nobody has written about yet. Disappearing tickers are acquisitions,
    delistings and bankruptcies.
    """
    if not previous_path.exists():
        return
    try:
        old = pd.read_csv(previous_path, usecols=["Ticker", "Issuer Name"])
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not diff against previous run: %s", exc)
        return

    old_set = set(old["Ticker"].dropna())
    new_set = set(df["Ticker"].dropna())

    added = df[df["Ticker"].isin(new_set - old_set)].copy()
    removed = old[old["Ticker"].isin(old_set - new_set)].copy()

    added_cols = [c for c in
                  ["Ticker", "Issuer Name", "Exchange", "Sector",
                   "Industry (SIC Description)", "Public Float ($M)",
                   "Revenue ($M)", "First EDGAR Filing", "Flags"]
                  if c in added.columns]

    rows = []
    for _, r in added[added_cols].iterrows():
        rows.append({"Change": "added", **r.to_dict()})
    for _, r in removed.iterrows():
        rows.append({
            "Change": "removed",
            "Ticker": r["Ticker"],
            "Issuer Name": r.get("Issuer Name", ""),
        })

    out = DATA_DIR / "changes.csv"
    pd.DataFrame(rows).to_csv(out, index=False)
    log.info("Wrote %s — %d added, %d removed since last run", out, len(added), len(removed))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    df = derive(load())
    df = order_columns(df)

    sort_key = "Public Float ($M)" if "Public Float ($M)" in df.columns else "Revenue ($M)"
    df = df.sort_values(sort_key, ascending=False, na_position="last")

    csv_path = ROOT / "us_listed_companies.csv"
    xlsx_path = ROOT / "us_listed_companies.xlsx"

    write_changes(df, csv_path)

    df.to_csv(csv_path, index=False)
    write_excel(df, xlsx_path)

    with_revenue = int(df["Revenue ($M)"].notna().sum()) if "Revenue ($M)" in df else 0
    with_float = int(df["Public Float ($M)"].notna().sum()) if "Public Float ($M)" in df else 0
    flagged = int(pd.to_numeric(df["Flag Count"], errors="coerce").fillna(0).gt(0).sum())

    log.info("Wrote %s and %s", csv_path.name, xlsx_path.name)
    log.info(
        "%d companies | %d with revenue | %d with a public float | %d flagged",
        len(df), with_revenue, with_float, flagged,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
