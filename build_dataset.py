"""
build_dataset.py  —  STEP 5 of the pipeline

Joins the four layers, computes the ratios you would otherwise compute by
hand, and writes the browsable output.

No network access at all. This step is pure local computation, so you can
re-run it as often as you like while you tune what you want to see.

Outputs
-------
us_listed_companies.csv   full dataset, one row per listed equity
us_listed_companies.xlsx  same data with frozen headers, autofilters,
                          sensible number formats and a data-dictionary tab
data/universe_snapshot.csv  dated copy, so you can diff week over week
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
    for name, key in (("profiles.csv", "CIK"), ("fundamentals.csv", "CIK")):
        part = read(name)
        if part.empty:
            continue
        part[key] = pd.to_numeric(part[key], errors="coerce").astype("Int64")
        part = part.drop_duplicates(subset=[key])
        df = df.merge(part, on=key, how="left", suffixes=("", f"_{name[:4]}"))

    prices = read("prices.csv")
    if not prices.empty:
        prices = prices.drop_duplicates(subset=["Ticker"])
        df = df.merge(prices, on="Ticker", how="left")

    log.info("Joined dataset: %d rows x %d columns", len(df), len(df.columns))
    return df


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------

def num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce")


def ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    den = denominator.replace(0, np.nan)
    return numerator / den


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
    price = num(out, "Price")

    fcf = ocf - capex
    total_debt = ltd + cud
    liquid = cash + sti
    market_cap = price * shares
    # Fall back to public float when we have no price. It understates market
    # cap (it excludes insider-held shares) but it is the right order of
    # magnitude and it comes straight from the filing.
    size_proxy = market_cap.fillna(public_float)
    ev = market_cap + total_debt - liquid

    out["Shares Outstanding (M)"] = (shares / MILLION).round(2)
    out["Market Cap ($M)"] = (market_cap / MILLION).round(1)
    out["Public Float ($M)"] = (public_float / MILLION).round(1)
    out["Size Proxy ($M)"] = (size_proxy / MILLION).round(1)
    out["Enterprise Value ($M)"] = (ev / MILLION).round(1)

    out["Revenue ($M)"] = (revenue / MILLION).round(1)
    out["Revenue FY-1 ($M)"] = (revenue_1 / MILLION).round(1)
    out["Revenue FY-2 ($M)"] = (revenue_2 / MILLION).round(1)
    out["Net Income ($M)"] = (net_income / MILLION).round(1)
    out["Operating Income ($M)"] = (op_income / MILLION).round(1)
    out["Gross Profit ($M)"] = (gross_profit / MILLION).round(1)
    out["Operating Cash Flow ($M)"] = (ocf / MILLION).round(1)
    out["CapEx ($M)"] = (capex / MILLION).round(1)
    out["Free Cash Flow ($M)"] = (fcf / MILLION).round(1)
    out["R&D ($M)"] = (rnd / MILLION).round(1)
    out["Stock Comp ($M)"] = (sbc / MILLION).round(1)
    out["Total Assets ($M)"] = (assets / MILLION).round(1)
    out["Shareholders Equity ($M)"] = (equity / MILLION).round(1)
    out["Cash & Investments ($M)"] = (liquid / MILLION).round(1)
    out["Total Debt ($M)"] = (total_debt / MILLION).round(1)
    out["Net Cash ($M)"] = ((liquid - total_debt) / MILLION).round(1)
    out["Retained Earnings ($M)"] = (retained / MILLION).round(1)

    # --- margins and returns ---
    out["Gross Margin %"] = pct(ratio(gross_profit, revenue))
    out["Operating Margin %"] = pct(ratio(op_income, revenue))
    out["Net Margin %"] = pct(ratio(net_income, revenue))
    out["FCF Margin %"] = pct(ratio(fcf, revenue))
    out["R&D % of Revenue"] = pct(ratio(rnd, revenue))
    out["Stock Comp % of Revenue"] = pct(ratio(sbc, revenue))
    out["Return on Equity %"] = pct(ratio(net_income, equity))
    out["Return on Assets %"] = pct(ratio(net_income, assets))

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
    out["Net Debt / FCF"] = ratio(total_debt - liquid, fcf).round(2)
    out["Interest Coverage"] = ratio(op_income, interest).round(2)

    # --- valuation ---
    out["P/E"] = ratio(market_cap, net_income.where(net_income > 0)).round(2)
    out["P/S"] = ratio(market_cap, revenue).round(2)
    out["P/B"] = ratio(market_cap, equity.where(equity > 0)).round(2)
    out["EV/Sales"] = ratio(ev, revenue).round(2)
    out["EV/EBIT"] = ratio(ev, op_income.where(op_income > 0)).round(2)
    out["FCF Yield %"] = pct(ratio(fcf, market_cap))
    out["Dividend Yield %"] = pct(ratio(dividends, market_cap))
    out["Buyback Yield %"] = pct(ratio(buybacks, market_cap))
    out["Rule of 40"] = (
        out["Revenue Growth 1Y %"].fillna(0) + out["FCF Margin %"].fillna(0)
    ).round(1)

    # --- simple boolean handles for filtering in Excel ---
    out["Profitable"] = np.where(net_income > 0, "Y", np.where(net_income.notna(), "N", ""))
    out["FCF Positive"] = np.where(fcf > 0, "Y", np.where(fcf.notna(), "N", ""))
    out["Growing"] = np.where(
        out["Revenue Growth 1Y %"] > 0, "Y",
        np.where(out["Revenue Growth 1Y %"].notna(), "N", ""),
    )
    out["Net Cash Positive"] = np.where(
        (liquid - total_debt) > 0, "Y",
        np.where((liquid - total_debt).notna(), "N", ""),
    )

    out = add_flags(out, equity, retained, diluted, diluted_1)
    out["Data Completeness %"] = completeness(out)
    return out


def text(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series("", index=df.index, dtype="object")
    return df[col].fillna("").astype(str).str.strip()


def add_flags(out: pd.DataFrame, equity, retained, diluted, diluted_1) -> pd.DataFrame:
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
        flags = np.where(
            (flags != "") & (part != ""), joined,
            np.where(flags != "", flags, part),
        )
        flags = pd.Series(flags, index=out.index)

    out["Flags"] = flags
    out["Flag Count"] = count
    return out


KEY_FIELDS = [
    "Revenue ($M)", "Net Income ($M)", "Total Assets ($M)",
    "Shareholders Equity ($M)", "Operating Cash Flow ($M)",
    "Shares Outstanding (M)", "Price", "Industry (SIC Description)",
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
    # size and valuation
    "Price", "Price Date", "Market Cap ($M)", "Public Float ($M)",
    "Size Proxy ($M)", "Enterprise Value ($M)", "Shares Outstanding (M)",
    "P/E", "P/S", "P/B", "EV/Sales", "EV/EBIT", "FCF Yield %",
    "Dividend Yield %", "Buyback Yield %",
    # income statement
    "Revenue ($M)", "Revenue FY-1 ($M)", "Revenue FY-2 ($M)",
    "Gross Profit ($M)", "Operating Income ($M)", "Net Income ($M)",
    "EPSDiluted", "R&D ($M)", "Stock Comp ($M)",
    # margins, returns, growth
    "Gross Margin %", "Operating Margin %", "Net Margin %", "FCF Margin %",
    "Return on Equity %", "Return on Assets %", "R&D % of Revenue",
    "Stock Comp % of Revenue",
    "Revenue Growth 1Y %", "Revenue CAGR 2Y %", "Net Income Growth 1Y %",
    "Latest Qtr Revenue ($M)", "Qtr Revenue YoY %", "Rule of 40",
    "Share Count Change 1Y %",
    # cash flow and balance sheet
    "Operating Cash Flow ($M)", "CapEx ($M)", "Free Cash Flow ($M)",
    "Total Assets ($M)", "Shareholders Equity ($M)", "Cash & Investments ($M)",
    "Total Debt ($M)", "Net Cash ($M)", "Retained Earnings ($M)",
    "Current Ratio", "Debt / Equity", "Net Debt / FCF", "Interest Coverage",
    # price behaviour
    "52W High", "52W Low", "Pct Off 52W High", "Pct Above 52W Low",
    "200D MA", "Pct Above 200D MA", "Avg Dollar Volume 20D",
    "Return 3M %", "Return 12M %",
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
    ("Size Proxy ($M)", "Market cap when a price is available, otherwise SEC EntityPublicFloat."),
    ("P/E", "Market cap / net income. Blank when net income is zero or negative."),
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
    "All figures come from company filings and are historical. Nothing here is a recommendation.",
    "Annual figures are the most recent fiscal year each company reported, so different rows can cover different periods. Check 'Revenue Period End'.",
    "Financial-sector filers (banks, insurers, REITs) often leave Revenue and Gross Profit blank because their income statements use different XBRL tags.",
    "Prices come from a free third-party source and may lag or be missing. SEC data is authoritative; price-derived columns are not.",
    "A blank cell means the company did not report that tag, not that the value is zero.",
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
            elif name in ("Issuer Name", "SEC Company Name", "Industry (SIC Description)", "Flags", "Security Name", "Former Names"):
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
        row = 3
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
# Main
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
                   "Industry (SIC Description)", "Size Proxy ($M)",
                   "Revenue ($M)", "First EDGAR Filing", "Flags"]
                  if c in added.columns]

    rows = []
    for _, r in added[added_cols].iterrows():
        rows.append({"Change": "added", **r.to_dict()})
    for _, r in removed.iterrows():
        rows.append({"Change": "removed", "Ticker": r["Ticker"], "Issuer Name": r.get("Issuer Name", "")})

    out = DATA_DIR / "changes.csv"
    pd.DataFrame(rows).to_csv(out, index=False)
    log.info("Wrote %s — %d added, %d removed since last run", out, len(added), len(removed))


def main() -> int:
    df = derive(load())
    df = order_columns(df)

    sort_key = "Size Proxy ($M)" if "Size Proxy ($M)" in df.columns else "Ticker"
    df = df.sort_values(sort_key, ascending=False, na_position="last")

    csv_path = ROOT / "us_listed_companies.csv"
    xlsx_path = ROOT / "us_listed_companies.xlsx"

    write_changes(df, csv_path)

    df.to_csv(csv_path, index=False)
    write_excel(df, xlsx_path)

    filled = int(df["Revenue ($M)"].notna().sum()) if "Revenue ($M)" in df else 0
    priced = int(df["Price"].notna().sum()) if "Price" in df else 0
    log.info("Wrote %s and %s", csv_path.name, xlsx_path.name)
    log.info(
        "%d companies | %d with revenue | %d with a price | %d flagged",
        len(df), filled, priced,
        int(pd.to_numeric(df.get("Flag Count"), errors="coerce").fillna(0).gt(0).sum()),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
