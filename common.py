"""Shared helpers: config, env, HTTP with retry, and JSONL files.

Every collector imports from here so they all behave the same way.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
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


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    with open(ROOT / path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env() -> None:
    """Read .env from the project folder, if present. Keys are never logged."""
    load_dotenv(ROOT / ".env")


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
    last_err: Exception | None = None
    for attempt in range(1, max_tries + 1):
        try:
            resp = session.get(url, params=params, headers=hdrs, timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            log.warning("request failed (%s), retry %d/%d in %.0fs", e, attempt, max_tries, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue

        if resp.status_code in (403, 429):
            wait = _rate_limit_wait(resp) or delay
            remaining = resp.headers.get("X-RateLimit-Remaining")
            log.warning(
                "HTTP %s (rate limit remaining=%s). Waiting %.0fs, retry %d/%d",
                resp.status_code, remaining, wait, attempt, max_tries,
            )
            if attempt == max_tries:
                raise RateLimited(f"gave up after {max_tries} tries: {resp.status_code} {resp.text[:200]}")
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue

        if resp.status_code >= 500:
            log.warning("HTTP %s, retry %d/%d in %.0fs", resp.status_code, attempt, max_tries, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue

        resp.raise_for_status()
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
