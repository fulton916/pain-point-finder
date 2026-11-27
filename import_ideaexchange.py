# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Import a hand-made IdeaExchange CSV into the common record format.

We do not scrape IdeaExchange. You make data/ideaexchange.csv yourself with these columns:

    title, url, points, votes, status, posted_date, comments, substatus

1 vote = 10 points on IdeaExchange. `votes` is the score. If a row has points but no votes,
votes is derived as points / 10 and the row is logged, so nothing is silently guessed.
`substatus` is optional (issue #31): the site's second status line (Partial, Not Planned, Complete,
Building, Submitted, Never, On Roadmap, Beta Testing). It goes into the body so it prints under the
row; only the main `status` drives scoring.status_weights.

Writes data/raw/ideaexchange.jsonl.

Run:            python import_ideaexchange.py
Test with the made-up sample (writes a separate file that is never ranked):
                python import_ideaexchange.py --sample
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from common import (
    ROOT,
    load_config,
    log,
    make_record,
    now_iso,
    read_jsonl,
    setup_logging,
    validate_record,
    write_jsonl,
)

SOURCE = "ideaexchange"
REQUIRED_COLUMNS = ["title", "url", "points", "votes", "status", "posted_date", "comments"]
OPTIONAL_COLUMNS = ["substatus"]
# Printed in the search header (issue #31): the CSV has a posted date only (2006 to 2016 for the
# current rows), and voting on an open idea goes on for years, so the --since window is not applied.
NOT_DATE_FILTERED = "IdeaExchange rows are not date-filtered (the CSV has posted dates only)."


def search(session, cfg: dict, query: str, since, **options) -> list[dict]:
    """Local search over the imported CSV (data/raw/ideaexchange.jsonl): every query word must
    appear in the title. No network.

    `since` is ignored (issue #31, option a): the CSV holds the posted date only, and an idea posted
    in 2007 may still be gathering votes, so a date window would hide every row. The header says so
    through the `info` option (list[str]), the same hook collect_reddit.py uses.

    A query word equal to the config `topic` (Salesforce) is skipped: every idea in the CSV is about
    the topic, and its titles never repeat the word, so "salesforce reports" means "reports" here."""
    path = ROOT / cfg["paths"]["raw_dir"] / "ideaexchange.jsonl"
    if not path.exists():
        # Part of the answer is missing, so the header says so (`notes`, like a missing YouTube key)
        # and ppf.py does not cache the empty result.
        why = f"ideaexchange skipped: {path.name} not found; run import_ideaexchange.py first."
        log.warning("%s", why)
        if options.get("notes") is not None:
            options["notes"].append(why)
        return []
    if options.get("info") is not None:
        options["info"].append(NOT_DATE_FILTERED)
    topic = str(cfg.get("topic") or "").strip().lower()
    words = [w for w in query.lower().split() if w and w != topic]
    if topic and len(words) < len(query.split()):
        log.info("ideaexchange: skipped the topic word %r (every idea in the CSV is about it)", topic)
    records = []
    for rec in read_jsonl(path):
        title = (rec.get("title") or "").lower()
        if not all(w in title for w in words):
            continue
        records.append(rec)
    records.sort(key=lambda r: (-(r["score"] if r["score"] != "" else -1), r["url"]))
    log.info("ideaexchange %r: %d records (local file)", query, len(records))
    return records


def to_int(value: str) -> int | str:
    """'1,234' -> 1234. Empty or non-numeric -> '' (never guess)."""
    s = (value or "").strip().replace(",", "")
    if not s:
        return ""
    try:
        return int(float(s))
    except ValueError:
        return ""


def row_to_record(row: dict[str, str], fetched_at: str, line_no: int) -> dict:
    votes = to_int(row.get("votes", ""))
    points = to_int(row.get("points", ""))
    if votes == "" and points != "":
        votes = points // 10
        log.info("line %d: votes empty, derived %d votes from %d points", line_no, votes, points)
    body = ""
    if points != "":
        body = f"points: {points}"
    substatus = (row.get("substatus") or "").strip()
    if substatus:
        # Shown under the row (ppf.py prints the body as the snippet); the weight comes from `status` only.
        body = f"{body}; sub-status: {substatus}" if body else f"sub-status: {substatus}"
    return make_record(
        source=SOURCE,
        url=(row.get("url") or "").strip(),
        title=(row.get("title") or "").strip(),
        body=body,
        score=votes,
        comments=to_int(row.get("comments", "")),
        views="",  # IdeaExchange does not show view counts
        created_at=(row.get("posted_date") or "").strip(),
        last_activity="",  # not in the CSV; do not guess
        status=(row.get("status") or "").strip(),
        fetched_at=fetched_at,
    )


def import_csv(csv_path: Path, out_path: str) -> int:
    fetched_at = now_iso()
    records = []
    bad = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"{csv_path} is missing columns: {missing}. Expected: {REQUIRED_COLUMNS}")
        for line_no, row in enumerate(reader, start=2):
            rec = row_to_record(row, fetched_at, line_no)
            problems = validate_record(rec)
            if problems:
                bad += 1
                log.warning("line %d skipped: %s", line_no, problems)
                continue
            records.append(rec)
    records.sort(key=lambda r: (-(r["score"] if r["score"] != "" else -1), r["url"]))
    n = write_jsonl(out_path, records)
    log.info("wrote %d records to %s (%d rows skipped)", n, out_path, bad)
    return n


def main(argv: list[str]) -> int:
    setup_logging()
    cfg = load_config()
    ie = cfg["ideaexchange"]
    raw_dir = cfg["paths"]["raw_dir"]
    if "--sample" in argv:
        log.warning("SAMPLE MODE: importing made-up rows to a file that merge_and_score.py never reads")
        csv_path = ROOT / ie["sample_csv_path"]
        out = f"{raw_dir}/ideaexchange_SAMPLE.jsonl"
    else:
        csv_path = ROOT / ie["csv_path"]
        out = f"{raw_dir}/ideaexchange.jsonl"
    if not csv_path.exists():
        print(f"No CSV at {csv_path}.")
        print(f"Create it with columns: {', '.join(REQUIRED_COLUMNS)}")
        print("Or try the importer on the sample:  python import_ideaexchange.py --sample")
        return 1
    import_csv(csv_path, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
