# Pain Point Finder: Build Brief for Claude Code

Paste this whole file into Claude Code as your first message. Or save it as `CLAUDE.md` in an empty project folder, then start Claude Code in that folder.

---

## Goal

Build a small Python tool that collects user complaints and feature requests with real vote counts. Then rank them so I can pick problems worth building apps for.

I am new to GitHub and Python. Explain each step in plain language. Tell me exactly what to type when I need to do something.

## Target topic

Default topic: **Salesforce** (admins, developers, Flow, reports, data export).
Put the topic and keywords in `config.yaml` so I can change them later without editing code.

## Scope for version 1 (build only this)

Use only free, official APIs. Do NOT build anything for Reddit in version 1.

1. **Stack Exchange API v2.3**, `site=salesforce`. Get top-voted questions from the last 24 months. Needs a free key from stackapps.com.
2. **Hacker News Algolia API** (`hn.algolia.com/api/v1/search`). No key needed. Search the topic keywords with `points>50`.
3. **GitHub API**. Search issues in repos related to the topic. Sort by reactions. Needs a free personal access token.
4. **IdeaExchange import**. Do not scrape it. Read a CSV that I will make by hand or with Claude in Chrome (`data/ideaexchange.csv`).

## Before you write code

1. Check each API's current official docs for rate limits and terms. My research said:
   - Stack Exchange: 10,000 requests/day with a key; 30 requests/second max; attribution required (CC BY-SA).
   - HN Algolia: 10,000 requests/hour per IP; 1,000 hits per query.
   - GitHub: 5,000 requests/hour with a token; search is 30 requests/minute and 1,000 results per search.
   Tell me if any of these have changed.
2. Show me the folder layout and plan. Wait for my OK.

## Required file layout

```
pain-point-finder/
  config.yaml          # topic, keywords, date range, repos to search
  .env                 # API keys (never committed)
  .gitignore           # must include .env and data/raw/
  collect_se.py
  collect_hn.py
  collect_gh.py
  import_ideaexchange.py
  merge_and_score.py
  data/raw/            # one JSONL file per source
  data/output/         # ranked results
  README.md            # how to run it, in plain steps
```

## Common record format (every source)

Each script writes JSON lines with these fields:

`source, url, title, body, score, comments, views, created_at, last_activity, status, fetched_at`

Leave a field empty if the source does not have it. Never guess a value.

## IdeaExchange CSV columns

`title, url, points, votes, status, posted_date, comments`

Note: 1 vote = 10 points on IdeaExchange. Use `votes` as the score.

## Scoring (merge_and_score.py)

1. **Dedupe.** Exact URL match first. Then merge near-duplicate titles with simple text similarity. Keep the highest score. Record every source it appeared in.
2. **Normalize.** Convert each score to a percentile rank within its own source. Votes from different sites are not comparable.
3. **Weight.**
   - Recency: halve the weight every 12 months since last activity.
   - Comments: small bonus.
   - Cross-source: bonus when the same problem shows up in 2+ sources.
4. **Output** `data/output/ranked.csv` with: title, best URL, source count, sources list, top normalized score, latest activity date, evidence label.
5. **Evidence label.**
   - "Strong": 2+ sources, OR top 10% in its source with activity in the last 24 months.
   - "Moderate": top 25% in one source.
   - "Anecdotal": everything else.

## Theme step

After scoring, read the top 100 rows of `ranked.csv`. Group them into problem themes. For each theme write:
- one-line problem statement
- top 3 evidence links with their scores
- evidence label
- whether the vendor may have already fixed it (check IdeaExchange status or release notes if possible; say "not checked" if you didn't)

Save as `data/output/themes.md`.

## Rules

- Respect rate limits. Add a delay between requests and retry with backoff on errors.
- Never put keys in code or in a commit. Use `.env`.
- Do not scrape Reddit, Stack Overflow web pages, or IdeaExchange pages. Use APIs or my CSV only.
- Label what is a fact from the data vs. your interpretation.
- After it works, help me create a private GitHub repo named `pain-point-finder` and push the code. Walk me through each git command.

## Later (not version 1)

- Arctic Shift archive for r/salesforce and r/SalesforceDeveloper (non-commercial research only; may stop working after March 2027).
- Review sites (G2, Capterra) via manual CSV.
- A simple dashboard view of `ranked.csv`.
