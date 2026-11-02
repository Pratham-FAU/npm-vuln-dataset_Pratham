"""Small helpers shared by the data sources: HTTP with caching/retries and date math."""

import gzip
import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
USER_AGENT = "pcom-npm-vuln-dataset (university course project)"


def _ssl_context():
    # python.org builds on macOS ship without CA certificates; fall back to certifi or the
    # system bundle so HTTPS works without running "Install Certificates.command".
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for bundle in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(bundle):
            return ssl.create_default_context(cafile=bundle)
    return ssl.create_default_context()


SSL_CONTEXT = _ssl_context()


class HttpError(Exception):
    def __init__(self, status, url, body=""):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.body = body


def http_get(url, headers=None, retries=6):
    """GET a URL and return (status, headers, body bytes).
    Retries on 5xx, network errors and 429 (too many requests), waiting longer each time."""
    req_headers = {"User-Agent": USER_AGENT}
    req_headers.update(headers or {})
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < retries:
                retry_after = e.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else 5 * 2 ** attempt
                print(f"    server says too many requests, waiting {min(wait, 300)} s ...")
                time.sleep(min(wait, 300))
                continue
            if e.code >= 500 and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise HttpError(e.code, url, body) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < min(retries, 3):  # give up quickly when offline
                time.sleep(2 ** attempt)
                continue
            raise


def _cache_path(key):
    return CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json.gz")


def read_cached_json(key):
    """Cached value for key, or None if it was never downloaded."""
    path = _cache_path(key)
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def cached_json(key, fetch):
    """Return fetch() result, caching it as gzipped JSON under cache/ by key."""
    path = _cache_path(key)
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    data = fetch()
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


# ---------- dates ----------

def parse_ts(s):
    """Parse an ISO-8601 timestamp (with Z or offset) into an aware UTC datetime."""
    if s is None:
        return None
    s = s.strip().replace("Z", "+00:00")
    # Python accepts at most 6 fractional digits; OSV uses 9.
    if "." in s:
        head, rest = s.split(".", 1)
        digits = ""
        while rest and rest[0].isdigit():
            digits, rest = digits + rest[0], rest[1:]
        s = f"{head}.{digits[:6].ljust(6, '0')}{rest}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ts(dt):
    """RFC 3339, UTC, exactly three fractional digits: 2026-04-08T18:39:24.455Z"""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def utc_date(y, m, d=1):
    return datetime(y, m, d, tzinfo=timezone.utc)


def add_months(dt, n):
    total = dt.year * 12 + (dt.month - 1) + n
    return dt.replace(year=total // 12, month=total % 12 + 1, day=1)


def month_windows(prediction):
    """[(start, end), ...] for M0..M11. M0 is the calendar month right before the
    prediction date, M11 the month eleven months before that. End is exclusive."""
    return [(add_months(prediction, -(m + 1)), add_months(prediction, -m)) for m in range(12)]


def month_index(ts, prediction):
    """Index m (0..11) of the month window containing ts, or None."""
    for m, (start, end) in enumerate(month_windows(prediction)):
        if start <= ts < end:
            return m
    return None


def days_between(a, b):
    return (b - a).days


OUTCOME_DAYS = 90
OUTCOME = timedelta(days=OUTCOME_DAYS)
