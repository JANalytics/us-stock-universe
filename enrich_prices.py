"""
enrich_prices.py  —  STEP 4 of the pipeline  (optional)

SEC filings give you the business. They do not give you the price, and without
a price you cannot compute a market cap or any valuation multiple.

There is no government price feed. The free options are all private sites with
no published rate limits and no service guarantee. This module uses Stooq,
which serves plain CSV without a key or an account. Treat it as best-effort:
if it breaks, the rest of the dataset is unaffected and you still have
EntityPublicFloat from the SEC as a rough size proxy.

Two request patterns, deliberately:

  1. Latest quote — Stooq's light quote endpoint accepts many symbols in one
     URL, so ~5,000 tickers costs ~70 requests instead of 5,000.

  2. Daily history — one request per ticker, needed for 52-week range,
     200-day average and trailing returns. This is the expensive one, so it
     runs on a budget: PRICE_HISTORY_BUDGET tickers per run, longest-stale
     first, everything cached. Week one fills part of the market, week two
     fills more, and from then on it just tops up. No single run ever looks
     like a crawl.

Set ENABLE_PRICES=0 to skip this step entirely.

Output: data/prices.csv
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import sys

from common import (
    DATA_DIR,
    BudgetExhausted,
    HttpClient,
    chunked,
    setup_logging,
    to_float,
)

log = setup_logging("prices")

STOOQ_HOST = "stooq.com"
STOOQ_QUOTE_URL = "https://stooq.com/q/l/?s={symbols}&f=sd2t2ohlcv&h&e=csv"
STOOQ_HISTORY_URL = "https://stooq.com/q/d/l/?s={symbol}&d1={start}&d2={end}&i=d"

QUOTE_BATCH_SIZE = int(os.environ.get("QUOTE_BATCH_SIZE", "60"))
PRICE_HISTORY_BUDGET = int(os.environ.get("PRICE_HISTORY_BUDGET", "2500"))
HISTORY_TTL_DAYS = float(os.environ.get("HISTORY_TTL_DAYS", "14"))
QUOTE_TTL_HOURS = float(os.environ.get("QUOTE_TTL_HOURS", "12"))
STOOQ_RATE = float(os.environ.get("STOOQ_RATE_PER_SEC", "1.5"))

EQUITY_TYPES = {"Common Stock", "Ordinary Shares", "ADR"}

FIELDNAMES = [
    "Ticker",
    "Price",
    "Price Date",
    "Volume",
    "52W High",
    "52W Low",
    "Pct Off 52W High",
    "Pct Above 52W Low",
    "200D MA",
    "Pct Above 200D MA",
    "Avg Dollar Volume 20D",
    "Return 3M %",
    "Return 12M %",
    "History Days",
    "History Updated",
]


def stooq_symbol(ticker: str) -> str:
    return ticker.strip().lower().replace(".", "-").replace("/", "-") + ".us"


def load_tickers() -> list[str]:
    path = DATA_DIR / "universe.csv"
    if not path.exists():
        raise SystemExit("data/universe.csv missing — run build_universe.py first.")
    out = []
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["Security Type"] in EQUITY_TYPES and row.get("CIK"):
                out.append(row["Ticker"])
    return out


def load_previous() -> dict[str, dict]:
    path = DATA_DIR / "prices.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return {r["Ticker"]: r for r in csv.DictReader(fh)}


def parse_quotes(raw: bytes) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not raw:
        return out
    text = raw.decode("utf-8", errors="replace")
    for row in csv.DictReader(io.StringIO(text)):
        symbol = (row.get("Symbol") or "").strip().lower()
        if not symbol:
            continue
        close = to_float(row.get("Close"))
        if close is None:
            continue
        out[symbol] = {
            "Price": close,
            "Price Date": (row.get("Date") or "").strip(),
            "Volume": to_float(row.get("Volume")) or "",
        }
    return out


def parse_history(raw: bytes) -> list[tuple[str, float, float, float, float]]:
    """Returns [(date, high, low, close, volume)] oldest first."""
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    if "Date" not in text[:200]:
        return []
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        close = to_float(row.get("Close"))
        if close is None:
            continue
        rows.append(
            (
                (row.get("Date") or "").strip(),
                to_float(row.get("High")) or close,
                to_float(row.get("Low")) or close,
                close,
                to_float(row.get("Volume")) or 0.0,
            )
        )
    rows.sort()
    return rows


def _pct(a, b):
    if a is None or b in (None, 0):
        return ""
    return round((a / b - 1.0) * 100.0, 2)


def summarise_history(rows) -> dict:
    if not rows:
        return {}
    closes = [r[3] for r in rows]
    window = rows[-252:] if len(rows) >= 2 else rows
    high52 = max(r[1] for r in window)
    low52 = min(r[2] for r in window)
    last = closes[-1]

    ma200 = sum(closes[-200:]) / len(closes[-200:]) if len(closes) >= 200 else None
    adv = (
        sum(r[3] * r[4] for r in rows[-20:]) / min(20, len(rows))
        if rows
        else None
    )

    ret3m = _pct(last, closes[-64]) if len(closes) > 64 else ""
    ret12m = _pct(last, closes[-252]) if len(closes) > 252 else ""

    return {
        "52W High": round(high52, 4),
        "52W Low": round(low52, 4),
        "Pct Off 52W High": _pct(last, high52),
        "Pct Above 52W Low": _pct(last, low52),
        "200D MA": round(ma200, 4) if ma200 else "",
        "Pct Above 200D MA": _pct(last, ma200) if ma200 else "",
        "Avg Dollar Volume 20D": round(adv, 0) if adv else "",
        "Return 3M %": ret3m,
        "Return 12M %": ret12m,
        "History Days": len(rows),
    }


def main() -> int:
    if os.environ.get("ENABLE_PRICES", "1").strip() in {"0", "false", "no"}:
        log.info("ENABLE_PRICES is off — skipping price enrichment.")
        return 0

    today = dt.date.today()
    tickers = load_tickers()
    previous = load_previous()

    client = HttpClient(
        # Stooq is not the SEC; it just wants a normal, identifiable client.
        user_agent=os.environ.get(
            "PRICE_USER_AGENT",
            "us-stock-universe research script (github.com/JANalytics/us-stock-universe)",
        ),
        default_rate=STOOQ_RATE,
        max_requests=int(os.environ.get("MAX_PRICE_REQUESTS", "8000")),
        max_consecutive_failures=15,
        timeout=60.0,
    )
    client.set_rate(STOOQ_HOST, STOOQ_RATE)

    results: dict[str, dict] = {t: {"Ticker": t} for t in tickers}

    # ---- 1. batched latest quotes -----------------------------------------
    log.info("Fetching latest quotes for %d tickers in batches of %d",
             len(tickers), QUOTE_BATCH_SIZE)
    quote_hits = 0
    for batch in chunked(tickers, QUOTE_BATCH_SIZE):
        symbols = "+".join(stooq_symbol(t) for t in batch)
        try:
            raw = client.get(
                STOOQ_QUOTE_URL.format(symbols=symbols),
                cache_ttl=QUOTE_TTL_HOURS * 3600,
                allow_404=True,
            )
        except BudgetExhausted as exc:
            log.error("Stopping quotes early: %s", exc)
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("quote batch failed: %s", exc)
            continue

        quotes = parse_quotes(raw)
        for ticker in batch:
            q = quotes.get(stooq_symbol(ticker))
            if q:
                results[ticker].update(q)
                quote_hits += 1

    log.info("Got quotes for %d/%d tickers", quote_hits, len(tickers))

    # ---- 2. budgeted daily history ----------------------------------------
    def staleness(t: str) -> tuple:
        prev = previous.get(t, {})
        return (bool(prev.get("History Updated")), prev.get("History Updated", ""))

    ordered = sorted(tickers, key=staleness)
    start = (today - dt.timedelta(days=500)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    fetched = 0
    log.info("Refreshing daily history for up to %d tickers this run", PRICE_HISTORY_BUDGET)

    for ticker in ordered:
        if fetched >= PRICE_HISTORY_BUDGET:
            break
        url = STOOQ_HISTORY_URL.format(symbol=stooq_symbol(ticker), start=start, end=end)
        try:
            raw = client.get(url, cache_ttl=HISTORY_TTL_DAYS * 86_400, allow_404=True)
        except BudgetExhausted as exc:
            log.error("Stopping history fetch early: %s", exc)
            break
        except Exception as exc:  # noqa: BLE001
            log.debug("history %s failed: %s", ticker, exc)
            continue

        summary = summarise_history(parse_history(raw))
        if summary:
            summary["History Updated"] = today.isoformat()
            results[ticker].update(summary)
            fetched += 1
            if fetched % 250 == 0:
                log.info("  history %d/%d — %s", fetched, PRICE_HISTORY_BUDGET, client.stats())

    # Carry forward anything we did not refresh this run.
    carry = [f for f in FIELDNAMES if f not in ("Ticker", "Price", "Price Date", "Volume")]
    for ticker, row in results.items():
        prev = previous.get(ticker)
        if not prev:
            continue
        for field in carry:
            if not row.get(field) and prev.get(field):
                row[field] = prev[field]
        for field in ("Price", "Price Date", "Volume"):
            if not row.get(field) and prev.get(field):
                row[field] = prev[field]

    out = DATA_DIR / "prices.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for ticker in sorted(results):
            writer.writerow({k: results[ticker].get(k, "") for k in FIELDNAMES})

    log.info("Wrote %s — %d tickers, %d histories refreshed", out, len(results), fetched)
    log.info(client.stats())
    return 0


if __name__ == "__main__":
    sys.exit(main())
