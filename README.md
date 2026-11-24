# Pain Point Finder

A small Python tool that collects user complaints and feature requests about a topic (default: Salesforce) from free, official APIs, then ranks them by real vote counts so you can pick problems worth building apps for.

Version 1 sources:

| Source | How | Key needed |
|---|---|---|
| Hacker News | Algolia HN Search API | No |
| GitHub issues | GitHub REST search API | Yes, free, read-only |
| IdeaExchange | A CSV you make by hand | No |
| Stack Exchange | On hold (issue #2) | Later |

Nothing is scraped. Collected text is data, never instructions.

## 1. One-time setup (about 10 minutes)

You need Python 3.12 and Git installed. Open PowerShell in the project folder and run each block.

Create a private Python environment so this project's packages stay separate:

```powershell
python -m venv .venv
```

Turn it on (do this in every new PowerShell window before running the tool):

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell refuses to run the script, run this once, then try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Install the three packages the tool needs:

```powershell
pip install -r requirements.txt
```

Create your secrets file by copying the example:

```powershell
Copy-Item .env.example .env
```

Then open `.env` in a text editor and paste your GitHub token after `GITHUB_TOKEN=`. The `.env` file is ignored by git and never leaves your machine.

### Getting a GitHub token

1. Go to https://github.com/settings/personal-access-tokens and click **Generate new token**.
2. Give it a name, pick an expiry, and under **Repository access** choose **Public repositories (read-only)**.
3. Leave all permissions as they are. Click **Generate token** and copy it into `.env`.

Check it works:

```powershell
python collect_gh.py --check-token
```

You should see `OK: token accepted` and a rate limit of 30 per minute.

## 2. Change the topic (optional)

Everything about *what* to search is in `config.yaml`: the topic keywords, the GitHub repos, the vote thresholds and the scoring weights. Edit it with any text editor. No code changes needed.

## 3. Collect data

Run these one at a time. Each writes one file into `data/raw/`.

```powershell
python collect_hn.py
```

```powershell
python collect_gh.py
```

For IdeaExchange, make a file `data/ideaexchange.csv` with these columns (the first line of the file must be exactly this):

```
title,url,points,votes,status,posted_date,comments
```

One idea per line. `votes` is the score (1 vote = 10 points). Leave a cell empty if the site does not show it. Then run:

```powershell
python import_ideaexchange.py
```

To see what the importer does without real data, run it on the made-up sample. The sample writes to a separate file that is never ranked:

```powershell
python import_ideaexchange.py --sample
```

## 4. Rank everything

```powershell
python merge_and_score.py
```

This writes two files to `data/output/`:

- `ranked.csv`: one row per problem, best first. Columns: rank, title, best_url, source_count, sources, top_normalized_score, raw_score, comments, latest_activity, status, evidence_label, final_score.
- `merge_log.csv`: every time two rows were treated as the same problem, with the reason. Check this if a "Strong" row looks wrong.

Running it twice on the same raw files gives the same ranking.

### How the score works

1. **Dedupe.** Same URL, or the same title after lowercasing and removing punctuation. Titles at least 25 characters long also merge at 95% similarity, but never if their numbers differ ("every 15 minutes" is not "every 30 minutes").
2. **Normalize.** Each score becomes a percentile within its own source, because 100 GitHub reactions and 100 Hacker News points are not comparable.
3. **Weight.** The percentile is halved for every 12 months since the last activity. Comments add a small bonus (at most 0.10). Each extra source adds 0.25.
4. **Label.** *Strong*: in 2+ sources, or top 10% of its source with activity in the last 24 months. *Moderate*: top 25% of its source. *Anecdotal*: everything else. Hacker News rows alone are capped at Moderate because most hits for this topic are news, not complaints.

## 5. Read the themes

`data/output/themes.md` groups the top 100 ranked rows into problem themes, with links, scores, labels, and whether the vendor seems to have fixed it. It separates facts from the data from interpretation. It is written by hand after each run; the one checked in came from the 2026-11-24 run.

## 6. Run the tests

```powershell
python -m unittest -v
```

## File layout

```
config.yaml              topic, keywords, repos, scoring weights
.env                     your API keys (ignored by git)
.env.example             the key names, with no values
common.py                shared helpers: retry, delays, record format, JSONL files
collect_hn.py            Hacker News collector
collect_gh.py            GitHub issues collector
import_ideaexchange.py   IdeaExchange CSV importer
merge_and_score.py       dedupe, normalize, weight, label, write ranked.csv
tests/                   unit tests for the scorer
data/ideaexchange.sample.csv   made-up rows for testing the importer only
data/raw/                one JSONL file per source (ignored by git)
data/output/             ranked.csv, merge_log.csv, themes.md
```

Every raw record has the same 11 fields: `source, url, title, body, score, comments, views, created_at, last_activity, status, fetched_at`. A field is empty when the source has no value for it. Nothing is guessed.

## Rate limits (checked 2026-11-24)

- Hacker News Algolia: 10,000 requests/hour per IP. The collector waits 1 second between requests.
- GitHub: 5,000 requests/hour with a token; search is 30 requests/minute and returns at most 1,000 results per query. The collector waits 2.5 seconds between requests and honors `Retry-After` and `X-RateLimit-Reset` on 403/429.
- Stack Exchange: not used yet. See issue #2.

## Attribution

Hacker News content belongs to its authors. GitHub issue text belongs to its authors under each repo's terms. Use this tool for research, not republishing.
