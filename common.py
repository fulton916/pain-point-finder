# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Shared helpers: config, env, HTTP with retry, and JSONL files.

Every collector imports from here so they all behave the same way.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests
import yaml
from dotenv import load_dotenv

# The 11 fields every record has, in this order. Empty string when a source has no value.
FIELDS = [
    "source",
    "url",
    "title",
    "body",
    "score",
    "comments",
    "views",
    "created_at",
    "last_activity",
    "status",
    "fetched_at",
]

ROOT = Path(__file__).resolve().parent
USER_AGENT = "pain-point-finder (research tool)"

log = logging.getLogger("ppf")

# Counts HTTP requests made through get_json, so a run can report "0 requests, all from cache".
STATS = {"requests": 0}
# Search mode asks the sources from several threads at once; the counter is shared.
_STATS_LOCK = threading.Lock()

# Secret values (API keys sent as query parameters) that must never appear in a log line or an
# error message. load_env() adds the values of SECRET_NAMES, a collector may add its key again;
# redact() masks them in anything we print.
SECRET_NAMES = ("GITHUB_TOKEN", "STACKEXCHANGE_KEY", "YOUTUBE_API_KEY")
SECRETS: set[str] = set()


def redact(text: str) -> str:
    for secret in SECRETS:
        if secret:
            text = text.replace(secret, "***")
    return text


_TAG = re.compile(r"<[^>]+>")


def clean_html(text: str) -> str:
    """Keep the words of an HTML fragment, drop the tags. Paragraphs and line breaks become newlines."""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</p>\s*<p>|<p>", "\n\n", text)
    return html.unescape(_TAG.sub("", text)).strip()


# Letters and digits in any script (\w minus the underscore), so accented words survive.
_WORD = re.compile(r"[^\W_]+")


def words_of(text: str) -> list[str]:
    """Lower-case words (letters and digits, any script) in order, repeats kept: "VA claim, rating?" -> va claim rating."""
    return _WORD.findall((text or "").lower())


def has_phrase(words: list[str], phrase: str) -> bool:
    """True when every word of `phrase` appears in `words` in that order, as whole words (so a one-word
    phrase is a whole-word match and "va claim" needs both words next to each other). Case does not
    matter: `words` comes from words_of and `phrase` goes through it here. An empty phrase never matches."""
    key_words = words_of(phrase)
    n = len(key_words)
    return bool(n) and any(words[i:i + n] == key_words for i in range(len(words) - n + 1))


def setup_logging(level: int = logging.INFO) -> None:
    # Windows consoles default to cp1252, which cannot print many titles (emoji, curly quotes).
    # Switch stdout and stderr to UTF-8 so output never crashes on a character.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, AttributeError):
                pass
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr
    )


_SINCE = re.compile(r"^(\d+)\s*([dwmy])$")


def parse_since(text: str, now: datetime | None = None) -> datetime:
    """'24m' = 24 months ago, '30d' days, '8w' weeks, '2y' years, or a YYYY-MM-DD date. Always UTC."""
    now = now or datetime.now(timezone.utc)
    s = (text or "").strip().lower()
    m = _SINCE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "d":
            return now - timedelta(days=n)
        if unit == "w":
            return now - timedelta(weeks=n)
        return _shift_months(now, n * (12 if unit == "y" else 1))
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"--since must look like 24m, 30d, 8w, 2y or YYYY-MM-DD, not {text!r}") from None


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    with open(ROOT / path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env() -> None:
    """Read .env from the project folder, if present. Keys are never logged: every key named in
    SECRET_NAMES that has a value is added to SECRETS so redact() masks it."""
    load_dotenv(ROOT / ".env")
    for name in SECRET_NAMES:
        value = env(name)
        if value:
            SECRETS.add(value)


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _shift_months(dt: datetime, months: int) -> datetime:
    """Same day of month, `months` earlier; the day is clamped when that month is shorter."""
    total = dt.year * 12 + (dt.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    last_day = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return dt.replace(year=year, month=month, day=min(dt.day, last_day))


def make_record(**values: Any) -> dict[str, Any]:
    """Build a record with all FIELDS present. Unknown fields raise so typos are caught."""
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown record fields: {sorted(unknown)}")
    rec = {f: "" for f in FIELDS}
    for k, v in values.items():
        rec[k] = "" if v is None else v
    return rec


def validate_record(rec: dict[str, Any]) -> list[str]:
    """Return a list of problems (empty list means valid)."""
    problems = []
    missing = [f for f in FIELDS if f not in rec]
    if missing:
        problems.append(f"missing fields: {missing}")
    extra = [k for k in rec if k not in FIELDS]
    if extra:
        problems.append(f"extra fields: {extra}")
    for f in ("source", "url", "title", "fetched_at"):
        if not rec.get(f):
            problems.append(f"{f} is empty")
    for f in ("score", "comments", "views"):
        v = rec.get(f, "")
        if v != "" and not isinstance(v, (int, float)):
            problems.append(f"{f} is not a number: {v!r}")
    return problems


def write_jsonl(path: str | Path, records: Iterable[dict[str, Any]]) -> int:
    """Write records as one JSON object per line. Returns the count written."""
    path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    path = ROOT / path
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


class RateLimited(Exception):
    pass


def get_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    max_tries: int = 6,
    timeout: int = 30,
) -> tuple[Any, requests.Response]:
    """GET a JSON URL with retries.

    Retries on connection errors and 5xx with exponential backoff.
    On 403/429 waits for Retry-After or X-RateLimit-Reset when present, else backs off.
    Raises for other 4xx (a real problem, not worth retrying).
    """
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    delay = 2.0
    last_err: str = "no attempts made"
    for attempt in range(1, max_tries + 1):
        last_try = attempt == max_tries
        try:
            with _STATS_LOCK:
                STATS["requests"] += 1
            resp = session.get(url, params=params, headers=hdrs, timeout=timeout)
        except requests.RequestException as e:
            last_err = redact(str(e))
            if not last_try:
                log.warning("request failed (%s), retry %d/%d in %.0fs", last_err, attempt, max_tries, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
            continue

        if resp.status_code in (403, 429):
            if attempt == max_tries:
                raise RateLimited(redact(f"gave up after {max_tries} tries: {resp.status_code} {resp.text[:200]}"))
            wait = _rate_limit_wait(resp) or delay
            remaining = resp.headers.get("X-RateLimit-Remaining")
            log.warning(
                "HTTP %s (rate limit remaining=%s). Waiting %.0fs, retry %d/%d",
                resp.status_code, remaining, wait, attempt, max_tries,
            )
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue

        if resp.status_code >= 500:
            last_err = f"HTTP {resp.status_code}"
            if not last_try:
                log.warning("HTTP %s, retry %d/%d in %.0fs", resp.status_code, attempt, max_tries, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
            continue

        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            # The default message carries the full URL, query string included. Re-raise without it.
            raise requests.HTTPError(f"HTTP {resp.status_code} {resp.reason} from {url}", response=resp) from None
        return resp.json(), resp

    raise RuntimeError(f"gave up after {max_tries} tries: {last_err}")


def _rate_limit_wait(resp: requests.Response) -> float | None:
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return max(1.0, float(ra)) + 1
        except ValueError:
            pass
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(1.0, float(reset) - time.time()) + 1
        except ValueError:
            pass
    return None
