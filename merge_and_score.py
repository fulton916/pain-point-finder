"""Merge all raw sources, dedupe, normalize, weight, label, and write the ranking.

Reads the files listed under paths.sources in config.yaml.
Writes:
    data/output/ranked.csv     the ranking
    data/output/merge_log.csv  every merge that happened, so a bad match can be spotted

The whole run is deterministic: the same raw files always give the same ranking.
"The current date" for recency is the latest fetched_at found in the data, not the clock,
so rerunning tomorrow on the same files gives the same output.

Run:  python merge_and_score.py
"""

from __future__ import annotations

import csv
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from common import ROOT, load_config, log, read_jsonl, setup_logging, validate_record

LABELS = ["Anecdotal", "Moderate", "Strong"]
RANKED_COLUMNS = [
    "rank",
    "title",
    "best_url",
    "source_count",
    "sources",
    "top_normalized_score",
    "raw_score",
    "comments",
    "latest_activity",
    "status",
    "evidence_label",
    "final_score",
]
MERGE_LOG_COLUMNS = [
    "reason",
    "similarity",
    "kept_source",
    "kept_url",
    "kept_title",
    "merged_source",
    "merged_url",
    "merged_title",
]


# ---------- normalizing ----------

def normalize_url(url: str) -> str:
    """Lowercase the host, drop the fragment and a trailing slash. Nothing else."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str) -> str:
    return _NON_WORD.sub(" ", title.lower()).strip()


def parse_date(value: Any) -> datetime | None:
    """Accept ISO 8601 (with Z or offset) or YYYY-MM-DD. Anything else is None."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def months_between(earlier: datetime, later: datetime) -> float:
    return max(0.0, (later - earlier).total_seconds() / (86400 * 30.4375))


def percentile_ranks(scores: list[float]) -> list[float]:
    """Fraction of items with a score <= this one. Top item is 1.0, ties share a value."""
    n = len(scores)
    if n == 0:
        return []
    ordered = sorted(scores)
    return [sum(1 for s in ordered if s <= x) / n for x in scores]


# ---------- grouping ----------

@dataclass
class Group:
    members: list[dict] = field(default_factory=list)
    norm_title: str = ""


def dedupe(records: list[dict], scoring: dict, merge_log: list[dict]) -> list[Group]:
    """Exact URL match first, then strict title match. Every merge is appended to merge_log."""
    # Pass 1: exact URL.
    by_url: dict[str, Group] = {}
    order: list[str] = []
    for rec in records:
        key = normalize_url(rec["url"])
        if key in by_url:
            g = by_url[key]
            _log_merge(merge_log, g.members[0], rec, "url_exact", 1.0)
            g.members.append(rec)
        else:
            by_url[key] = Group(members=[rec])
            order.append(key)

    # Pass 2: titles. Process highest-scoring groups first so the kept item is the strongest.
    groups = [by_url[k] for k in order]
    groups.sort(key=lambda g: (-_group_pct(g), g.members[0]["url"]))
    threshold = float(scoring["title_similarity_threshold"])
    min_len = int(scoring["title_min_length"])

    kept: list[Group] = []
    by_title: dict[str, Group] = {}
    for g in groups:
        g.norm_title = normalize_title(g.members[0]["title"])
        target = by_title.get(g.norm_title)
        reason, sim = "title_exact", 1.0
        if target is None and len(g.norm_title) >= min_len:
            target, sim = _most_similar(g.norm_title, kept, threshold, min_len)
            reason = "title_similar"
        if target is not None:
            for m in g.members:
                _log_merge(merge_log, target.members[0], m, reason, sim)
            target.members.extend(g.members)
        else:
            kept.append(g)
            by_title[g.norm_title] = g
    return kept


_DIGITS = re.compile(r"\d+")


def _most_similar(title: str, groups: list[Group], threshold: float, min_len: int) -> tuple[Group | None, float]:
    best, best_ratio = None, 0.0
    title_numbers = _DIGITS.findall(title)
    for g in groups:
        other = g.norm_title
        if len(other) < min_len:
            continue
        # "every 15 minutes" and "every 30 minutes" are different problems: numbers must match.
        if _DIGITS.findall(other) != title_numbers:
            continue
        # Cheap length check before the expensive comparison.
        if abs(len(other) - len(title)) / max(len(other), len(title)) > (1 - threshold):
            continue
        sm = SequenceMatcher(None, title, other)
        if sm.quick_ratio() < threshold:
            continue
        ratio = sm.ratio()
        if ratio >= threshold and ratio > best_ratio:
            best, best_ratio = g, ratio
    return best, best_ratio


def _group_pct(g: Group) -> float:
    return max(m.get("_pct", 0.0) for m in g.members)


def _log_merge(merge_log: list[dict], kept: dict, merged: dict, reason: str, similarity: float) -> None:
    merge_log.append({
        "reason": reason,
        "similarity": round(similarity, 4),
        "kept_source": kept["source"],
        "kept_url": kept["url"],
        "kept_title": kept["title"],
        "merged_source": merged["source"],
        "merged_url": merged["url"],
        "merged_title": merged["title"],
    })


# ---------- scoring ----------

def add_percentiles(records: list[dict]) -> None:
    """Attach _pct to each record: percentile rank of its score within its own source."""
    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_source[r["source"]].append(r)
    for source, recs in by_source.items():
        scored = [r for r in recs if r.get("score") != ""]
        for r in recs:
            r["_pct"] = 0.0
        pcts = percentile_ranks([float(r["score"]) for r in scored])
        for r, p in zip(scored, pcts):
            r["_pct"] = p
        if len(scored) < len(recs):
            log.warning("%s: %d records have no score and rank at the bottom", source, len(recs) - len(scored))


def latest_activity(members: list[dict]) -> datetime | None:
    dates = []
    for m in members:
        d = parse_date(m.get("last_activity")) or parse_date(m.get("created_at"))
        if d:
            dates.append(d)
    return max(dates) if dates else None


def label_for(source_count: int, top_pct: float, months_since: float | None, sources: list[str], scoring: dict) -> str:
    recent = months_since is not None and months_since <= float(scoring["strong_activity_months"])
    if source_count >= 2 or (top_pct >= float(scoring["strong_percentile"]) and recent):
        label = "Strong"
    elif top_pct >= float(scoring["moderate_percentile"]):
        label = "Moderate"
    else:
        label = "Anecdotal"
    # A row whose every source is capped can't rise above the lowest cap.
    caps = scoring.get("label_caps") or {}
    if sources and all(s in caps for s in sources):
        cap = min((caps[s] for s in sources), key=LABELS.index)
        if LABELS.index(label) > LABELS.index(cap):
            label = cap
    return label


def score_group(g: Group, scoring: dict, as_of: datetime) -> dict:
    members = g.members
    best = max(members, key=lambda m: (m["_pct"], m["score"] if m["score"] != "" else -1, m["url"]))
    sources = sorted({m["source"] for m in members})
    top_pct = best["_pct"]
    latest = latest_activity(members)
    months = months_between(latest, as_of) if latest else None

    half_life = float(scoring["recency_half_life_months"])
    recency = 0.5 ** (months / half_life) if months is not None else float(scoring["missing_date_weight"])
    comments = max((m["comments"] for m in members if m["comments"] != ""), default="")
    comment_bonus = min(float(scoring["comment_bonus_cap"]), float(scoring["comment_bonus_scale"]) * math.log1p(comments)) if comments != "" else 0.0
    cross_bonus = float(scoring["cross_source_bonus"]) * (len(sources) - 1)
    final = top_pct * recency + comment_bonus + cross_bonus

    return {
        "title": best["title"],
        "best_url": best["url"],
        "source_count": len(sources),
        "sources": ";".join(sources),
        "top_normalized_score": round(top_pct, 4),
        "raw_score": best["score"],
        "comments": comments,
        "latest_activity": latest.date().isoformat() if latest else "",
        "status": best.get("status", ""),
        "evidence_label": label_for(len(sources), top_pct, months, sources, scoring),
        "final_score": round(final, 4),
    }


def rank(records: list[dict], scoring: dict, as_of: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """Pure function: records in, (ranked rows, merge log) out. Used by main() and the tests."""
    records = [dict(r) for r in records]  # don't mutate the caller's data
    if as_of is None:
        as_of = max((parse_date(r.get("fetched_at")) for r in records), default=None) or datetime.now(timezone.utc)
    add_percentiles(records)
    merge_log: list[dict] = []
    groups = dedupe(records, scoring, merge_log)
    rows = [score_group(g, scoring, as_of) for g in groups]
    rows.sort(key=lambda r: (-r["final_score"], r["title"].lower(), r["best_url"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows, merge_log


# ---------- IO ----------

def load_records(paths: list[str]) -> list[dict]:
    records = []
    for p in paths:
        if not (ROOT / p).exists():
            log.warning("missing %s (collector not run yet?), skipping", p)
            continue
        n_bad = 0
        for rec in read_jsonl(p):
            problems = validate_record(rec)
            if problems:
                n_bad += 1
                continue
            records.append(rec)
        log.info("loaded %s (%d invalid skipped)", p, n_bad)
    return records


def write_csv(path: str, rows: list[dict], columns: list[str]) -> None:
    full = ROOT / path
    full.parent.mkdir(parents=True, exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    setup_logging()
    cfg = load_config()
    records = load_records(cfg["paths"]["sources"])
    if not records:
        print("No records found. Run a collector first (see README).")
        return 1
    rows, merge_log = rank(records, cfg["scoring"])

    out_dir = cfg["paths"]["output_dir"]
    write_csv(f"{out_dir}/ranked.csv", rows, RANKED_COLUMNS)
    write_csv(f"{out_dir}/merge_log.csv", merge_log, MERGE_LOG_COLUMNS)

    per_source = Counter(r["source"] for r in records)
    labels = Counter(r["evidence_label"] for r in rows)
    log.info("records per source: %s", dict(per_source))
    log.info("%d records -> %d ranked rows, %d merges logged", len(records), len(rows), len(merge_log))
    log.info("labels: %s", dict(labels))
    log.info("wrote %s/ranked.csv and %s/merge_log.csv", out_dir, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
