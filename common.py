"""
common.py
=========

Shared plumbing for the us-stock-universe pipeline.

Everything in this repo that touches the network goes through HttpClient.
That is deliberate: it means there is exactly one place that controls how
fast we hit a server, how we retry, and what we cache.

Three defences against getting blocked
--------------------------------------
1. Token-bucket rate limiting, per host. The SEC's published fair-access
   limit is 10 requests/second across all your machines. We default to 5.
2. On-disk caching with conditional revalidation (ETag / If-Modified-Since).
   A cached copy that is still inside its TTL costs zero requests. A stale
   copy usually costs one cheap 304.
3. A hard per-run request budget plus a circuit breaker. If something goes
   wrong, the run stops instead of hammering.

Nothing here needs an API key. Standard library plus `requests`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urlsplit

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", ROOT / ".cache"))
HTTP_CACHE_DIR = CACHE_DIR / "http"

for _d in (DATA_DIR, HTTP_CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(name: str) -> logging.Logger:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger(name)


log = logging.getLogger("common")


# ---------------------------------------------------------------------------
# User agent
# ---------------------------------------------------------------------------

def sec_user_agent() -> str:
    """
    The SEC requires automated clients to identify themselves with a real
    contact address. A generic or missing User-Agent is the single most
    common reason people get 403'd by data.sec.gov.

    Set the SEC_USER_AGENT repo secret to something like:
        "Jane Doe jane@example.com"
    """
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua or "example.com" in ua.lower():
        raise SystemExit(
            "SEC_USER_AGENT is not set (or still points at example.com).\n"
            "The SEC blocks unidentified automated traffic.\n"
            'Set it to something like: "Jane Doe jane@example.com"\n'
            "In GitHub: Settings -> Secrets and variables -> Actions -> New "
            "repository secret, named SEC_USER_AGENT."
        )
    return ua


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple thread-safe token bucket. `rate` is requests per second."""

    def __init__(self, rate: float, burst: float = 1.0):
        self.rate = max(rate, 0.01)
        self.capacity = max(burst, 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self.capacity, self._tokens + (now - self._last) * self.rate
            )
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.rate
                time.sleep(wait)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= 1.0

    def slow_down(self, factor: float = 0.5, floor: float = 0.5) -> None:
        """Called after a 429/403 so the rest of the run is gentler."""
        with self._lock:
            new_rate = max(self.rate * factor, floor)
            if new_rate < self.rate:
                log.warning(
                    "Backing off: rate %.2f -> %.2f req/s", self.rate, new_rate
                )
            self.rate = new_rate


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class BudgetExhausted(RuntimeError):
    """Raised when the run's request budget or failure threshold is hit."""


@dataclass
class HttpClient:
    user_agent: str
    default_rate: float = 5.0
    max_requests: int = 250_000
    max_consecutive_failures: int = 25
    timeout: float = 90.0
    cache_dir: Path = HTTP_CACHE_DIR

    _session: requests.Session = field(init=False, repr=False)
    _limiters: dict = field(default_factory=dict, init=False, repr=False)
    _rates: dict = field(default_factory=dict, init=False, repr=False)
    requests_made: int = field(default=0, init=False)
    cache_hits: int = field(default=0, init=False)
    _consecutive_failures: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": self.user_agent,
                # gzip matters: SEC JSON compresses ~10x. Less bandwidth for
                # them, faster for us, and it is what their docs ask for.
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            }
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- rate control -------------------------------------------------------

    def set_rate(self, host: str, rate: float) -> None:
        self._rates[host] = rate
        self._limiters.pop(host, None)

    def _limiter(self, host: str) -> RateLimiter:
        if host not in self._limiters:
            self._limiters[host] = RateLimiter(
                self._rates.get(host, self.default_rate)
            )
        return self._limiters[host]

    # -- cache --------------------------------------------------------------

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        sub = self.cache_dir / key[:2]
        sub.mkdir(parents=True, exist_ok=True)
        return sub / f"{key}.body", sub / f"{key}.meta.json"

    @staticmethod
    def _read_meta(path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    # -- main entry point ---------------------------------------------------

    def get(
        self,
        url: str,
        cache_ttl: float = 86_400.0,
        retries: int = 5,
        allow_404: bool = False,
    ) -> Optional[bytes]:
        """
        Fetch `url`, returning the body as bytes (or None on an allowed 404).

        cache_ttl: seconds a cached body is considered fresh. Inside the TTL
                   no network request happens at all. Set to 0 to always
                   revalidate, or to a huge number for immutable resources.
        """
        body_path, meta_path = self._cache_paths(url)
        meta = self._read_meta(meta_path)

        if body_path.exists() and meta:
            age = time.time() - meta.get("fetched_at", 0)
            if age < cache_ttl:
                self.cache_hits += 1
                if meta.get("status") == 404:
                    return None
                return body_path.read_bytes()

        host = urlsplit(url).netloc
        limiter = self._limiter(host)
        conditional = {}
        if body_path.exists():
            if meta.get("etag"):
                conditional["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                conditional["If-Modified-Since"] = meta["last_modified"]

        last_err: Optional[str] = None

        for attempt in range(1, retries + 1):
            if self.requests_made >= self.max_requests:
                raise BudgetExhausted(
                    f"Request budget of {self.max_requests} exhausted."
                )
            if self._consecutive_failures >= self.max_consecutive_failures:
                raise BudgetExhausted(
                    f"{self._consecutive_failures} consecutive failures; "
                    "stopping so we don't keep hammering the source."
                )

            limiter.acquire()
            self.requests_made += 1

            try:
                resp = self._session.get(
                    url, headers=conditional, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_err = repr(exc)
                self._consecutive_failures += 1
                self._sleep_backoff(attempt)
                continue

            status = resp.status_code

            if status == 304 and body_path.exists():
                self._consecutive_failures = 0
                meta["fetched_at"] = time.time()
                meta_path.write_text(json.dumps(meta))
                return body_path.read_bytes()

            if status == 200:
                self._consecutive_failures = 0
                body = resp.content
                body_path.write_bytes(body)
                meta_path.write_text(
                    json.dumps(
                        {
                            "url": url,
                            "status": 200,
                            "fetched_at": time.time(),
                            "etag": resp.headers.get("ETag"),
                            "last_modified": resp.headers.get("Last-Modified"),
                        }
                    )
                )
                return body

            if status == 404:
                # Cache the miss. Plenty of CIK/tag combinations legitimately
                # do not exist, and re-asking every week is wasted traffic.
                self._consecutive_failures = 0
                body_path.write_bytes(b"")
                meta_path.write_text(
                    json.dumps(
                        {"url": url, "status": 404, "fetched_at": time.time()}
                    )
                )
                if allow_404:
                    return None
                last_err = "404"
                break

            if status in (403, 429) or status >= 500:
                # 403 from the SEC usually means "you are being throttled",
                # not "forbidden forever". Slow the whole host down.
                if status in (403, 429):
                    limiter.slow_down()
                retry_after = resp.headers.get("Retry-After")
                delay = None
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = None
                last_err = f"HTTP {status}"
                self._consecutive_failures += 1
                self._sleep_backoff(attempt, fixed=delay)
                continue

            last_err = f"HTTP {status}"
            break

        if allow_404:
            log.debug("giving up on %s (%s)", url, last_err)
            return None
        raise RuntimeError(f"Failed to fetch {url}: {last_err}")

    def get_json(self, url: str, **kwargs) -> Any:
        raw = self.get(url, **kwargs)
        if raw is None or raw == b"":
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning("Bad JSON from %s: %s", url, exc)
            return None

    @staticmethod
    def _sleep_backoff(attempt: int, fixed: Optional[float] = None) -> None:
        if fixed is not None:
            delay = min(fixed, 120.0)
        else:
            # Exponential with jitter. Jitter matters: without it, parallel
            # or repeated runs synchronise and retry in lockstep.
            delay = min(2.0 ** attempt, 60.0) * (0.5 + random.random())
        time.sleep(delay)

    def stats(self) -> str:
        return (
            f"{self.requests_made} network requests, "
            f"{self.cache_hits} served from cache"
        )


def make_sec_client() -> HttpClient:
    """An HttpClient pre-configured for SEC hosts."""
    rate = float(os.environ.get("SEC_RATE_PER_SEC", "5"))
    client = HttpClient(
        user_agent=sec_user_agent(),
        default_rate=rate,
        max_requests=int(os.environ.get("MAX_REQUESTS", "250000")),
    )
    for host in ("www.sec.gov", "data.sec.gov", "sec.gov"):
        client.set_rate(host, rate)
    return client


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def chunked(seq: list, size: int) -> Iterator[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def write_json(path: Path, obj: Any) -> None:
    """Write atomically so an interrupted run never leaves a half file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def safe_div(num: Any, den: Any) -> Optional[float]:
    n, d = to_float(num), to_float(den)
    if n is None or d is None or d == 0:
        return None
    return n / d


# ---------------------------------------------------------------------------
# SIC -> sector
# ---------------------------------------------------------------------------

_SIC_DIVISIONS: list[tuple[int, int, str]] = [
    (100, 999, "Agriculture, Forestry & Fishing"),
    (1000, 1499, "Mining & Energy Extraction"),
    (1500, 1799, "Construction"),
    (2000, 3999, "Manufacturing"),
    (4000, 4999, "Transport, Utilities & Communications"),
    (5000, 5199, "Wholesale Trade"),
    (5200, 5999, "Retail Trade"),
    (6000, 6799, "Finance, Insurance & Real Estate"),
    (7000, 8999, "Services"),
    (9100, 9999, "Public Administration"),
]

# A handful of ranges people actually screen on, carved out of the broad
# divisions above because "Manufacturing" and "Services" are far too coarse.
_SIC_REFINEMENTS: list[tuple[int, int, str]] = [
    (2833, 2836, "Biotech & Pharma"),
    (8731, 8734, "Biotech & Pharma"),
    (3570, 3579, "Computer Hardware"),
    (3661, 3669, "Communications Equipment"),
    (3670, 3679, "Semiconductors & Electronics"),
    (3674, 3674, "Semiconductors"),
    (7370, 7379, "Software & IT Services"),
    (7372, 7372, "Software"),
    (6020, 6036, "Banks"),
    (6199, 6221, "Capital Markets"),
    (6311, 6411, "Insurance"),
    (6500, 6599, "Real Estate"),
    (6798, 6798, "REITs"),
    (1311, 1389, "Oil & Gas"),
    (4911, 4991, "Utilities"),
    (8000, 8093, "Healthcare Providers"),
    (3841, 3851, "Medical Devices"),
    (5812, 5813, "Restaurants"),
]


def sic_to_sector(sic: Any) -> str:
    try:
        code = int(str(sic).strip()[:4])
    except (TypeError, ValueError):
        return ""
    for lo, hi, name in _SIC_REFINEMENTS:
        if lo <= code <= hi:
            return name
    for lo, hi, name in _SIC_DIVISIONS:
        if lo <= code <= hi:
            return name
    return ""
