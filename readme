# US stock universe

A free, self-refreshing dataset of every operating company listed on a US
exchange, with enough fundamental, valuation and filing-behaviour data to
browse by hand and decide what's worth reading a 10-K about.

No API keys. No paid services. No accounts. Runs on GitHub Actions.

## What you get

`us_listed_companies.csv` and `us_listed_companies.xlsx` — roughly 5,000 rows,
one per listed equity, with about 100 columns grouped into:

| Group | Examples |
|---|---|
| Identity | ticker, issuer, exchange, sector, SIC industry, CIK, HQ city/state, incorporation state, filer category |
| Size & valuation | price, market cap, enterprise value, public float, P/E, P/S, P/B, EV/Sales, EV/EBIT, FCF yield, dividend & buyback yield |
| Income statement | revenue (3 years), gross profit, operating income, net income, diluted EPS, R&D, stock comp |
| Margins & returns | gross/operating/net/FCF margin, ROE, ROA, R&D and stock comp as % of revenue |
| Growth | revenue growth 1Y, revenue CAGR 2Y, net income growth, latest-quarter revenue YoY, Rule of 40, share count change |
| Cash flow & balance sheet | operating cash flow, capex, free cash flow, assets, equity, cash & investments, total debt, net cash, retained earnings, current ratio, debt/equity, net debt/FCF, interest coverage |
| Price behaviour | 52-week high/low, % off high, 200-day MA, 20-day average dollar volume, 3M and 12M returns |
| Filing behaviour | latest 10-K and 10-Q dates, months since annual report, first EDGAR filing, counts of 8-K / Form 4 / 424B / S-1 & S-3 / SC 13D&G in the last 12 months, late-filing notices |
| Flags | plain-language notes such as *accumulated deficit*, *negative book equity*, *late-filing notice*, *share count +20% in 1y* |

The Excel file opens with frozen headers, autofilters on every column and a
**Read Me** tab explaining what each derived column means.

`data/changes.csv` lists tickers that appeared or disappeared since the last
run — new listings, spin-offs, uplistings, delistings and acquisitions. For
finding companies nobody has written about yet, this is often the most useful
file in the repo.

## Data sources

Everything below is free, public, and usable without registering.

| Source | Used for | Requests per refresh |
|---|---|---|
| [Nasdaq Trader symbol directories](https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs) | ticker list, exchange, ETF flag, listing-compliance status | 2 |
| [SEC `company_tickers_exchange.json`](https://www.sec.gov/files/company_tickers_exchange.json) | ticker → CIK mapping | 2 |
| [SEC submissions API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | company metadata and filing history | ~1 per company |
| [SEC XBRL frames API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | all fundamentals | ~250 total, for the entire market |
| [Stooq](https://stooq.com) | prices and price history (optional) | batched; budgeted per run |

### Why the frames API matters

The obvious way to pull fundamentals from EDGAR is the `companyfacts`
endpoint: one request per company, each response several megabytes. For 5,000
companies that's 5,000 requests and multiple gigabytes every single refresh —
and it's exactly the access pattern that gets an IP throttled.

The **frames** API inverts the query. Instead of *all concepts for one
company*, it returns *one concept for every company that reported it in a
period*:

```
https://data.sec.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2024.json
```

The entire market's annual revenue is one request. The whole fundamentals
layer costs a couple of hundred requests instead of several thousand, and most
of those come back from cache on later runs.

Two quirks worth knowing:

- Companies with off-calendar fiscal years get slotted into whichever calendar
  frame fits best, so the pipeline pulls several periods and keeps the most
  recent value per company **by its actual period-end date**. That end date is
  preserved in the `Revenue Period End` and `Assets As Of` columns — always
  check it before comparing two companies.
- Revenue has no single canonical XBRL tag. Post-ASC-606 filers mostly use
  `RevenueFromContractWithCustomerExcludingAssessedTax`; older filings use
  `Revenues` or `SalesRevenueNet`. All are queried and coalesced by priority.

## How this avoids getting blocked

The SEC's [fair-access policy](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
is 10 requests per second per user, across all machines, and they ask that
automated clients identify themselves. Ignoring either gets you a temporary IP
block. Six things in this repo address that:

1. **One choke point.** Every outbound request in the project goes through
   `HttpClient` in `common.py`. There is exactly one place that controls pace.
2. **Token-bucket rate limiting, per host.** Default 5 req/s to the SEC — half
   the published ceiling — and 1.5 req/s to Stooq, which publishes no limit at
   all and therefore deserves more caution, not less.
3. **A declared User-Agent.** `SEC_USER_AGENT` must contain a real name and
   email. The scripts refuse to start without it, because an unidentified
   client is the single most common reason people get 403'd by `data.sec.gov`.
4. **Aggressive caching with conditional revalidation.** Responses are stored
   on disk with their `ETag` and `Last-Modified`. Inside its TTL a cached
   response costs zero requests; past it, usually one cheap `304`. Immutable
   things like closed historical periods are cached for 60 days. The Actions
   cache carries this between runs, which is what makes the steady-state
   refresh so much lighter than a cold one.
5. **Backoff that actually backs off.** `429`, `403` and `5xx` trigger
   exponential backoff with jitter, `Retry-After` is honoured, and a throttled
   host has its rate *permanently halved for the rest of the run*. Jitter
   matters: without it, retries synchronise and arrive in lockstep.
6. **Budgets and a circuit breaker.** Each run has a hard request ceiling, and
   25 consecutive failures aborts the step rather than hammering a source
   that's already unhappy. The expensive per-ticker price history runs on a
   per-run budget (stalest first), so it fills in over a few weeks and no
   single run ever looks like a crawl.

The workflow also uses a `concurrency` group, so two runs can never overlap and
silently double the request rate.

## Running it

### On GitHub

1. Add a repository secret named `SEC_USER_AGENT` under
   **Settings → Secrets and variables → Actions**, formatted like
   `Jane Doe jane@example.com`.
2. That's it. The workflow runs every Saturday and commits the refreshed files.
   You can also trigger it by hand from the **Actions** tab.

### Locally

```bash
pip install -r requirements.txt
export SEC_USER_AGENT="Jane Doe jane@example.com"

python build_universe.py       # 4 requests
python enrich_profiles.py      # ~1 per company, cached for a week
python enrich_fundamentals.py  # ~250 requests for the whole market
python enrich_prices.py        # optional; set ENABLE_PRICES=0 to skip
python build_dataset.py        # no network at all
```

A cold first run takes roughly 30–60 minutes, almost all of it in
`enrich_profiles.py`. Later runs are much faster because of the cache.

`build_dataset.py` never touches the network, so you can re-run it as often as
you like while tuning what you want to see.

### Useful knobs

| Variable | Default | Effect |
|---|---|---|
| `SEC_USER_AGENT` | — | **Required.** Your name and email |
| `SEC_RATE_PER_SEC` | `5` | Requests per second to SEC hosts (ceiling is 10) |
| `PROFILE_TTL_DAYS` | `7` | How long a cached company profile stays fresh |
| `MAX_PROFILE_FETCHES` | `12000` | Per-run cap on profile requests |
| `ANNUAL_YEARS` | `4` | Annual frames to pull |
| `INSTANT_QUARTERS` | `6` | Balance-sheet frames to pull |
| `ENABLE_PRICES` | `1` | Set to `0` to skip Stooq entirely |
| `PRICE_HISTORY_BUDGET` | `2500` | Per-run cap on price-history requests |
| `LOG_LEVEL` | `INFO` | Set to `DEBUG` to see individual request failures |

## Reading the data honestly

A few things that will otherwise mislead you:

- **Blank ≠ zero.** A blank cell means the company didn't report that XBRL tag.
  Financial-sector filers (banks, insurers, REITs) routinely leave `Revenue`
  and `Gross Profit` empty because their income statements use different tags.
  Don't filter them out by accident and then conclude there are no cheap banks.
- **Different rows cover different periods.** Annual figures are each
  company's most recent reported fiscal year. Check `Revenue Period End`.
- **Annual figures are not trailing-twelve-month figures.** A company that
  reported FY2025 in February is nine months stale by autumn. The
  `Latest Qtr Revenue` and `Qtr Revenue YoY %` columns exist to give you a
  freshness check, not a replacement.
- **Prices are the weak link.** Stooq is a free third party with no
  availability guarantee. Every price-derived column (market cap, all the
  multiples, the return columns) inherits that uncertainty. The SEC columns
  are authoritative; these are not. `Public Float` from the filing cover page
  is the fallback size measure when a price is missing.
- **Flags are observations, not verdicts.** A clinical-stage biotech trips
  *accumulated deficit* and *4+ prospectuses in 12m* by design — that's what
  the business model looks like. The flags are there to make unusual rows easy
  to notice while scrolling.
- **This is a starting point for research, not a screen you should act on.**
  Everything here is historical data pulled from filings, with no adjustment
  for restatements, share-class overlap or one-off items.

## Notes on what was deliberately left out

- **IPO dates.** The SEC maintains no universal structured IPO-date field.
  The earlier version of this project searched S-1 and F-1 text for explicit
  language, which cost thousands of multi-megabyte document downloads to
  populate a column that was blank for most companies anyway. `First EDGAR
  Filing` is given instead, clearly labelled as what it is: the date the
  company first filed with the SEC, which is usually close to but not the same
  as the listing date.
- **TTM figures.** Frames give one fact per company per period, and Q4 revenue
  isn't separately reported by 10-K filers, so a reliable trailing-twelve-month
  series can't be assembled this way without per-company `companyfacts` calls.
  Annual figures plus a latest-quarter column is the honest version.
- **Insider buy/sell direction.** Form 4 *counts* are free from the submissions
  API; the direction of each trade requires downloading each filing. Counts
  measure activity, not sentiment, and the column is named accordingly.

## Repository layout

```
common.py                 shared HTTP client: rate limiting, caching, retries
build_universe.py         step 1 — ticker list and CIK mapping
enrich_profiles.py        step 2 — SEC company metadata and filing behaviour
enrich_fundamentals.py    step 3 — XBRL frames fundamentals
enrich_prices.py          step 4 — Stooq prices (optional)
build_dataset.py          step 5 — join, derive, write CSV + XLSX
requirements.txt
.github/workflows/refresh-dataset.yml
```

Each step writes a CSV into `data/` and reads only the steps before it, so any
one of them can be re-run on its own without redoing the others.

## Other free sources worth adding later

- **SEC Financial Statement Data Sets** (`sec.gov/dera/data`) — quarterly ZIPs
  of every number in every filing. Heavier to parse than frames, but complete,
  and it's one download per quarter.
- **SEC bulk archives** — `www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip`
  and `www.sec.gov/Archives/edgar/xbrl/companyfacts.zip` replace thousands of
  requests with a single large download. Worth switching to if the per-company
  profile step ever becomes the bottleneck.
- **FRED** (`fred.stlouisfed.org`) — free with a no-cost API key. Useful if you
  want macro series alongside the company data.
- **Treasury FiscalData API** — free, no key, for yield curves.

## Licence and disclaimer

The code is yours to do as you like with. The data comes from public filings
and free third-party sources and is provided as-is, with no warranty of
accuracy or completeness. Nothing in this repository is investment advice.
