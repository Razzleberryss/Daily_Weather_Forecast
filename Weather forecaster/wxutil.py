"""Shared utilities: HTTP fetching, unit conversion, and date handling.

Zero external dependencies — uses only the Python standard library.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

USER_AGENT = "kaus-forecaster/1.0 (personal research tool; rasberrynolen31@gmail.com)"
LOCAL_TZ = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

DEFAULT_TIMEOUT = 30
RETRIES = 3
RETRY_BACKOFF = 2.0  # seconds, doubled each retry


class SourceError(Exception):
    """A data source could not be fetched or parsed."""


def http_get(url: str, *, timeout: int = DEFAULT_TIMEOUT, retries: int = RETRIES,
             headers: dict | None = None) -> bytes:
    """GET a URL with retries. Raises SourceError after exhausting retries."""
    req_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            # Don't retry client errors other than 429
            if isinstance(e, urllib.error.HTTPError) and e.code not in (429, 500, 502, 503, 504):
                break
            if attempt < retries - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise SourceError(f"GET {url} failed: {last_err}")


def http_get_json(url: str, **kwargs):
    """GET a URL and parse the body as JSON."""
    body = http_get(url, **kwargs)
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise SourceError(f"GET {url}: invalid JSON ({e})") from e


def http_get_text(url: str, **kwargs) -> str:
    return http_get(url, **kwargs).decode("utf-8", errors="replace")


def build_url(base: str, params: dict) -> str:
    return f"{base}?{urllib.parse.urlencode(params)}"


def c_to_f(celsius: float | None) -> float | None:
    if celsius is None:
        return None
    return celsius * 9.0 / 5.0 + 32.0


def now_local() -> datetime:
    return datetime.now(tz=LOCAL_TZ)


def today_local() -> date:
    return now_local().date()


def parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp (handles trailing Z) to an aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)


def parse_iso_interval(interval: str) -> tuple[datetime, datetime]:
    """Parse an NWS validTime like '2026-07-21T06:00:00+00:00/PT18H'
    into (start, end) aware datetimes."""
    start_s, _, dur_s = interval.partition("/")
    start = parse_iso(start_s)
    return start, start + parse_duration(dur_s)


def parse_duration(dur: str) -> timedelta:
    """Parse a subset of ISO-8601 durations used by the NWS API,
    e.g. 'PT18H', 'P1D', 'P1DT6H'."""
    if not dur.startswith("P"):
        raise ValueError(f"bad duration: {dur}")
    days = hours = minutes = 0
    body = dur[1:]
    time_part = ""
    if "T" in body:
        day_part, _, time_part = body.partition("T")
    else:
        day_part = body
    if day_part.endswith("D"):
        days = int(day_part[:-1] or 0)
    num = ""
    for ch in time_part:
        if ch.isdigit():
            num += ch
        elif ch == "H":
            hours = int(num or 0)
            num = ""
        elif ch == "M":
            minutes = int(num or 0)
            num = ""
        elif ch == "S":
            num = ""
    return timedelta(days=days, hours=hours, minutes=minutes)


def daterange(start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]
