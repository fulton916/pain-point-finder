# Pain Point Themes: Salesforce (top 100 of ranked.csv)

Generated 2026-11-24 from `data/output/ranked.csv` (238 rows: 213 GitHub issues, 25 Hacker News stories, 0 IdeaExchange rows because no CSV exists yet). Zero merges happened (see `merge_log.csv`), so every row is one item from one source.

**How to read this file.** Lines marked **Fact** come straight from the data (reaction counts, points, dates, open/closed status, labels computed by `merge_and_score.py`). Lines marked **Interpretation** are my grouping and my reading of what the problem is. The theme groupings themselves are interpretation.

**Item types.** Each item is tagged `complaint` (something is broken or painful), `request` (asking for a feature), `how-to` (a question, which hints at a gap), or `news/announcement` (a story or a vendor post, not a user pain point). Across the top 100: 37 complaints, 23 requests, 5 how-to, 35 news/announcement.

**Score key.** GitHub: `reactions N, pct P` (P is percentile within GitHub). Hacker News: `points N, pct P` (P within Hacker News). Label is the evidence label from ranked.csv.

**Vendor status.** "closed" and "open" are GitHub issue states on the fetch date (fact). Whether a closed issue was actually fixed, and release notes, were **not checked** unless stated.

---

## Three warnings before the themes (interpretation, backed by facts)

1. **All three "Strong" rows in the top 100 are vendor announcements or housekeeping, not user pain.** Fact: rank 2 (CLI credential security announcement), rank 8 ("Maintainer away until April 27th") and rank 24 ("The future of jsforce") are labeled Strong. Interpretation: reactions on announcements measure attention, not pain. Treat "Strong" as a vote signal only.
2. **Hacker News added no complaints.** Fact: all 22 Hacker News rows in the top 100 are news stories or launch posts; 7 of them (ranks 1, 11, 29, 33, 36, 41, 55) are unrelated products that mention Salesforce somewhere in their text. Interpretation: the Algolia search matches body text, not just titles. The Moderate cap worked as intended. A title-only filter would be a sensible version 2 change.
3. **No IdeaExchange data yet.** Fact: `data/ideaexchange.csv` does not exist. Interpretation: the admin-side pain (Flow, reports, data export) is missing from this run. Everything below is developer-tooling pain because the GitHub repos are developer tools.

---

## Theme 1: VS Code Salesforce extensions eat CPU, memory and time

**Problem (interpretation):** The official VS Code extension pack is slow to start, slow to deploy, and sometimes pins the CPU or keeps running after VS Code is closed.

Evidence (fact):
- Rank 10, complaint: [Extension causes high cpu load](https://github.com/forcedotcom/salesforcedx-vscode/issues/6737), reactions 8, pct 0.66, open, activity 2026-07-17, Anecdotal
- Rank 39, complaint: [Apex Autocomplete issues due to performance problems](https://github.com/forcedotcom/salesforcedx-vscode/issues/2095), reactions 23, pct 0.96, closed, activity 2024-08-27, Moderate
- Rank 28, complaint: [LWC extension causes high CPU and memory exhaustion in 65.12.1](https://github.com/forcedotcom/salesforcedx-vscode/issues/6758), reactions 6, pct 0.42, closed, activity 2026-04-02, Anecdotal

Also in theme: ranks 66 (deploy takes minutes), 67 (slow startup), 89 (processes keep running after close). All complaints.

**Evidence label:** Moderate (best in theme). Six items, all one source.
**Vendor fixed?** Ranks 28, 39, 66, 67, 89 are closed; rank 10 is open as of 2026-07. Whether the closes were fixes was not checked. Interpretation: recurrence across 2024 to 2026 suggests this comes back with each major version.

## Theme 2: VS Code extension gives wrong or stale feedback after deploys

**Problem (interpretation):** After deploying or fixing code, the Problems panel keeps old errors, hides new ones, or the output panel stops showing results. Developers can't trust what the editor tells them.

Evidence (fact):
- Rank 17, complaint: [Problems tab does not refresh when issues are addressed](https://github.com/forcedotcom/salesforcedx-vscode/issues/2608), reactions 11, pct 0.79, closed, activity 2026-01-08, Moderate
- Rank 79, complaint: [Issue on problem panel not get clear after successful deployment](https://github.com/forcedotcom/salesforcedx-vscode/issues/4904), reactions 13, pct 0.84, closed, activity 2023-07-17, Moderate
- Rank 16, complaint: [Execute Anonymous no longer outputs to Output panel](https://github.com/forcedotcom/salesforcedx-vscode/issues/6943), reactions 9, pct 0.71, closed, activity 2026-03-12, Anecdotal

Also in theme: ranks 53 (Aura language server holds a file open), 95 (failed deploy shows no errors), 96 (vendor spec for conflict detection, announcement).

**Evidence label:** Moderate. **Vendor fixed?** All listed issues are closed. Not checked against release notes. Interpretation: the same complaint recurs in 2023, 2024 and 2026, so each fix seems partial.

## Theme 3: The Apex Language Server is a closed box tied to one Java version

**Problem (interpretation):** The Apex language server ships as a compiled jar, requires a specific Java version, fails to start for some users, and offers no inline documentation. Developers want to fix or extend it and can't.

Evidence (fact):
- Rank 40, request: [Open Source the Apex Language Server](https://github.com/forcedotcom/salesforcedx-vscode/issues/305), reactions 10, pct 0.74, closed, activity 2025-02-06, Anecdotal
- Rank 42, request: [Support Java versions newer than 21](https://github.com/forcedotcom/salesforcedx-vscode/issues/6068), reactions 7, pct 0.59, closed, activity 2025-04-18, Anecdotal
- Rank 38, complaint: [Cannot run Apex Replay Debugger / error during Apex Language Server startup](https://github.com/forcedotcom/salesforcedx-vscode/issues/6366), reactions 7, pct 0.59, closed, activity 2025-06-30, Anecdotal

Also in theme: rank 77 (request for official inline documentation).

**Evidence label:** Anecdotal. **Vendor fixed?** All closed; not checked. Interpretation: the open-source request being closed probably means "won't do", which keeps the gap open for third parties.

## Theme 4: New Node.js releases break the CLI and jsforce

**Problem (interpretation):** A Node.js patch release (24.17.0) made CLI logins hang and jsforce requests never return. Everyone on the current Node picks it up at once.

Evidence (fact):
- Rank 5, complaint: [Node.js 24.17.0: jsforce request promise can hang with keep-alive transport](https://github.com/jsforce/jsforce/issues/1806), reactions 17, pct 0.90, closed, activity 2026-07-20, Moderate
- Rank 6, complaint: [CLI doesn't work with node v24.17.0 or later](https://github.com/forcedotcom/cli/issues/3586), reactions 12, pct 0.82, closed, activity 2026-08-07, Moderate

**Evidence label:** Moderate. Two items, both one source (GitHub), two repos. The scorer did not merge them because the titles differ, which is correct: they are two symptoms of one cause.
**Vendor fixed?** Both closed within weeks (fact). Interpretation: fixed. Short-lived but acute pain; a poor app target because it resolves itself.

## Theme 5: CLI authentication is changing under people's feet

**Problem (interpretation):** Salesforce is tightening how the CLI stores credentials and which connected app it uses. Teams using JWT or External Client Apps hit new failures.

Evidence (fact):
- Rank 2, news/announcement: [Announcement: Upcoming Breaking Changes - Enhancing CLI Credential Security](https://github.com/forcedotcom/cli/issues/3560), reactions 19, pct 0.93, open, activity 2026-10-27, **Strong**
- Rank 7, news/announcement: [Restricted usage of the Salesforce CLI connected app](https://github.com/forcedotcom/cli/issues/3368), reactions 11, pct 0.79, closed, activity 2026-08-25, Moderate
- Rank 34, complaint: [sf org create scratch fails with C-1016 when authenticated via External Client App (JWT)](https://github.com/forcedotcom/cli/issues/3515), reactions 5, pct 0.26, open, activity 2026-09-29, Anecdotal

**Evidence label:** Strong by the scorer's rule, but see warning 1: the Strong row is a vendor post. Interpretation: real pain is in the comments of rank 2 and in rank 34; the label overstates it.
**Vendor fixed?** Rank 2 is an in-progress vendor change (open). Rank 34 open. Not checked further.

## Theme 6: CLI install, update and deploy commands fail in confusing ways

**Problem (interpretation):** `sf update` returns HTTP 403, the `sfdx` binary disappears after an update, pushes fail with internal git errors, moved metadata breaks push, and production deploys demand undocumented flags.

Evidence (fact):
- Rank 57, complaint: [sfdx: No such file or directory](https://github.com/forcedotcom/cli/issues/2447), reactions 15, pct 0.88, closed, activity 2024-02-08, Moderate
- Rank 61, complaint: [sfdx push -> An internal error caused this command to fail (isomorphic-git)](https://github.com/forcedotcom/cli/issues/2194), reactions 14, pct 0.86, closed, activity 2024-01-05, Moderate
- Rank 20, complaint: [sf update fails: Error: HTTP 403: Invalid channel stable](https://github.com/forcedotcom/cli/issues/3458), reactions 11, pct 0.79, closed, activity 2025-11-21, Moderate

Also in theme: ranks 43 (`sf org open` opens wrong user, open), 76 (impossible to deploy to production), 82 (package installed list TypeError), 90 (push error when metadata is moved).

**Evidence label:** Moderate. Seven items. **Vendor fixed?** Six closed, one open (rank 43). Not checked against release notes. Interpretation: these are individual bugs, fixed one by one; the durable pain is "CLI updates are risky", which points at version pinning and rollback tooling.

## Theme 7: The sfdx-to-sf migration created years of churn

**Problem (interpretation):** Commands were renamed, deprecated and removed over 2023 to 2024. Third-party tools such as CumulusCI had to follow, and users had to rewrite scripts.

Evidence (fact):
- Rank 52, news/announcement: [sf (v2) is Generally Available](https://github.com/forcedotcom/cli/issues/2132), reactions 18, pct 0.92, closed, activity 2024-07-03, Moderate
- Rank 45, news/announcement: [Deprecated sfdx source/mdapi/org commands will be removed on November 6th, 2024](https://github.com/forcedotcom/cli/issues/2974), reactions 11, pct 0.79, closed, activity 2024-11-21, Moderate
- Rank 78, request: [Upgrade all SFDX Commands to SF Commands](https://github.com/SFDO-Tooling/CumulusCI/issues/3621), reactions 6, pct 0.42, closed, activity 2024-11-14, Anecdotal

Also in theme: ranks 83 and 85 (vendor transition announcements).

**Evidence label:** Moderate. **Vendor fixed?** The migration is complete per the announcements (fact: all closed). Interpretation: historical; low value as an app target now.

## Theme 8: Metadata files are too coarse for version control

**Problem (interpretation):** Custom Labels, objects and other metadata live in one big XML file each. Diffs are unreadable, merges conflict, and the Org Browser can only retrieve whole objects, not single fields. Teams want finer-grained files and better manifest tooling.

Evidence (fact):
- Rank 72, request: [More granular metadata instead of complex XMLs](https://github.com/forcedotcom/salesforcedx-vscode/issues/4650), reactions 19, pct 0.93, closed, activity 2024-02-27, Moderate
- Rank 19, request: [Separate each Custom Label entry on its own file when working with the "Source" format](https://github.com/forcedotcom/cli/issues/1159), reactions 14, pct 0.86, closed, activity 2025-12-15, Moderate
- Rank 98, request: [Add option to force:source:retrieve to exclude Managed Package's components](https://github.com/forcedotcom/cli/issues/87), reactions 13, pct 0.84, closed, activity 2023-08-01, Moderate

Also in theme: ranks 56 (managed package objects missing from Org Browser), 59 (Package.xml utilities / working sets), 69 (retrieve one custom field via Org Browser), 86 (support multi-package workspaces), 97 (Org Browser should update package.xml).

**Evidence label:** Moderate. Eight items, the most of any request-driven theme. **Vendor fixed?** All closed. Rank 19 was closed in late 2025; whether the "decomposed" Custom Labels format shipped was **not checked**. Interpretation: this is the strongest app-shaped opportunity in the data: a metadata splitter, merger and manifest builder that sits beside the CLI.

## Theme 9: Salesforce Inspector users want better query and export tools

**Problem (interpretation):** Power users of the Salesforce Inspector Reloaded extension want to query by thousands of IDs, run several SOQL queries in tabs, format queries, copy only filtered rows, export nested data more than one level deep, and have a dark mode.

Evidence (fact):
- Rank 99, request: [Extract deeply nested (more than 1 level) plan](https://github.com/forcedotcom/cli/issues/60), reactions 18, pct 0.92, closed, activity 2023-08-01, Moderate
- Rank 12, request: [[UI] Dark Mode](https://github.com/tprouvot/Salesforce-Inspector-reloaded/issues/64), reactions 7, pct 0.59, open, activity 2026-08-07, Anecdotal
- Rank 25, request: [[data-export] SOQL WHERE IN ("thousands of ids") - Helper](https://github.com/tprouvot/Salesforce-Inspector-reloaded/issues/394), reactions 6, pct 0.42, open, activity 2026-07-26, Anecdotal

Also in theme: ranks 74 (multiple SOQL tabs, closed), 87 (format SOQL, open), 88 (copy filtered records only, closed).

**Evidence label:** Moderate. **Vendor fixed?** Ranks 74, 88, 99 closed; 12, 25, 87 open. Not checked. Interpretation: the Inspector repo is small (10 issues with 5+ reactions), so low counts here still mean a lot relative to the repo. This is admin-adjacent pain and the closest thing in this run to the "data export" topic in the brief.

## Theme 10: jsforce carries deprecated, vulnerable dependencies and a long v2 migration

**Problem (interpretation):** jsforce 1.x depended on the deprecated `request` package with known CVEs, and the TypeScript v2 rewrite took years, leaving users stuck between versions.

Evidence (fact):
- Rank 49, complaint: [request module (jsforce dependency) is deprecated](https://github.com/jsforce/jsforce/issues/999), reactions 23, pct 0.96, closed, activity 2024-07-15, Moderate
- Rank 91, complaint: [Request package vulnerable to SSRF](https://github.com/jsforce/jsforce/issues/1312), reactions 22, pct 0.94, closed, activity 2023-10-02, Moderate
- Rank 24, news/announcement: [The future of jsforce](https://github.com/jsforce/jsforce/issues/1360), reactions 46, pct 1.00, closed, activity 2025-03-03, **Strong**

Also in theme: ranks 23 (csv-parse audit warning, 2026), 48 (Moving to 2.0), 63 (TS-2321 error from generated schema), 84 (V2 status), 92 (request CVE).

**Evidence label:** Strong by the scorer, but the Strong row is an announcement (warning 1). The complaints are Moderate. **Vendor fixed?** Fact: rank 24 says the Salesforce CLI team now maintains jsforce; the `request` issues are closed. Interpretation: largely resolved by v2; the 2026 csv-parse item shows dependency hygiene is still a recurring irritation.

## Theme 11: jsforce users can't tell if a session is still valid, or how to do common tasks

**Problem (interpretation):** Developers ask the same questions for years: how to check whether a connection is still alive, how to send email, how to upsert by Id, how to mock the API in tests. The library's docs leave these gaps.

Evidence (fact):
- Rank 70, how-to: [Check if connection is still valid](https://github.com/jsforce/jsforce/issues/699), reactions 11, pct 0.79, closed, activity 2024-06-28, Moderate
- Rank 30, how-to: [How to check if current connect is still valid or expired?](https://github.com/jsforce/jsforce/issues/833), reactions 7, pct 0.59, open, activity 2025-11-07, Anecdotal
- Rank 80, how-to: [Is there a sendEmail() method?](https://github.com/jsforce/jsforce/issues/136), reactions 7, pct 0.59, closed, activity 2024-05-20, Anecdotal

Also in theme: ranks 71 (mocking/stubbing), 100 (upsert using Id as external ID).

**Evidence label:** Moderate. **Vendor fixed?** Not checked. Fact: ranks 30 and 70 are the same question asked in 2017-era and later threads; the scorer did not merge them because the titles differ beyond the strict threshold (0.95). Interpretation: a correct non-merge, but a human sees one problem.

## Theme 12: jsforce breaks on large data volumes

**Problem (interpretation):** Streaming more than about a million Bulk API rows, long-running queries, and Streaming API subscriptions all drop or hang.

Evidence (fact):
- Rank 62, complaint: [Salesforce streaming API stops streaming after an interval](https://github.com/jsforce/jsforce/issues/857), reactions 9, pct 0.71, open, activity 2024-06-11, Anecdotal
- Rank 44, complaint: [BUG (with fix): RecordStream.Parsable Cannot Stream More Than ~1M Results from Bulk.query](https://github.com/jsforce/jsforce/issues/697), reactions 7, pct 0.59, open, activity 2025-05-21, Anecdotal
- Rank 73, complaint: [ECONNRESET](https://github.com/jsforce/jsforce/issues/285), reactions 7, pct 0.59, closed, activity 2024-07-15, Anecdotal

**Evidence label:** Anecdotal. **Vendor fixed?** Two of three still open (fact). Interpretation: a durable gap for anyone building Salesforce data pipelines in Node.

## Theme 13: Lightning Web Components developers want TypeScript and fewer tooling false alarms

**Problem (interpretation):** The top open LWC request is TypeScript authoring. Others hit wrong editor errors for dynamic components, ask about private class fields, and lost the local dev server.

Evidence (fact):
- Rank 35, request: [Add TypeScript support to LWC authoring format](https://github.com/salesforce/lwc/issues/2078), reactions 24, pct 0.97, open, activity 2024-10-25, Moderate
- Rank 51, complaint: [Seeing wrong Dynamic Components error in VS Code](https://github.com/salesforce/lwc/issues/3945), reactions 6, pct 0.42, open, activity 2025-09-08, Anecdotal
- Rank 65, request: [[Feature Request] Hook Onto VSCode Command Execution](https://github.com/forcedotcom/salesforcedx-vscode/issues/6378), reactions 6, pct 0.42, closed, activity 2025-08-05, Anecdotal

Also in theme: ranks 68 (local dev server deprecation, announcement), 94 (private fields with LightningElement, open).

**Evidence label:** Moderate. **Vendor fixed?** Rank 35 open since 2020 with no activity after 2024-10 (fact). Not checked for a 2025 or 2026 TypeScript beta. Interpretation: strong, durable demand with a thin tooling ecosystem around it.

## Theme 14: Apex testing and code-quality tooling is thin

**Problem (interpretation):** Developers want to define their own test suites in the editor, find dead Apex code, get correct coverage reports for CI, and not have tests fail because of unrelated platform rules.

Evidence (fact):
- Rank 60, request: [Ability to create my own test suite](https://github.com/forcedotcom/salesforcedx-vscode/issues/2186), reactions 10, pct 0.74, closed, activity 2024-08-27, Anecdotal
- Rank 46, request: [Apex unused methods and variables to find dead code](https://github.com/forcedotcom/salesforcedx-vscode/issues/1489), reactions 7, pct 0.59, closed, activity 2025-08-05, Anecdotal
- Rank 58, complaint: [force:source:deploy --coverageformatters cobertura | Incorrect class filename in Cobertura XML reports](https://github.com/forcedotcom/cli/issues/1725), reactions 5, pct 0.26, open, activity 2026-01-11, Anecdotal

**Evidence label:** Anecdotal. **Vendor fixed?** Ranks 46 and 60 closed, 58 open. Not checked. Interpretation: dead-code detection for Apex is a self-contained app idea; evidence here is thin, so check IdeaExchange and Stack Exchange before building.

## Theme 15: CumulusCI breaks on Python dependency drift

**Problem (interpretation):** Fresh installs of CumulusCI warn or fail because of upstream Python packages (pkg_resources, docutils), and some tasks loop or skip metadata.

Evidence (fact):
- Rank 32, complaint: [pkg_resources deprecation warning when running CCI commands](https://github.com/SFDO-Tooling/CumulusCI/issues/3907), reactions 6, pct 0.42, closed, activity 2026-04-07, Anecdotal
- Rank 50, complaint: [Unmanaged Metadata not deploying during 2GP Package Create](https://github.com/SFDO-Tooling/CumulusCI/issues/3563), reactions 5, pct 0.26, closed, activity 2026-05-22, Anecdotal
- Rank 93, complaint: [Running CumulusCI commands fail from dependency docutils version 0.22.0](https://github.com/SFDO-Tooling/CumulusCI/issues/3912), reactions 5, pct 0.26, closed, activity 2025-08-06, Anecdotal

Also in theme: rank 54 (create_community loops/timeouts).

**Evidence label:** Anecdotal. **Vendor fixed?** All closed. Not checked. Interpretation: maintenance noise, not an app opportunity.

## Theme 16: Platform-side changes and outages break developer workflows

**Problem (interpretation):** Changes Salesforce makes on the platform (email domain verification, release-preview capacity, a global outage) surface as developer tooling failures nobody can fix locally.

Evidence (fact):
- Rank 9, complaint: [Apex tests that send email are failing due to domain verification requirement](https://github.com/forcedotcom/cli/issues/3534), reactions 12, pct 0.82, closed, activity 2026-05-07, Moderate
- Rank 3, news/announcement: [Salesforce Global Outage](https://news.ycombinator.com/item?id=49724488), points 280, pct 0.88, 184 comments, 2026-09-16, Moderate (Hacker News, capped)
- Rank 81, news/announcement: [Capacity shortages during release preview](https://github.com/forcedotcom/cli/issues/1690), reactions 23, pct 0.96, closed, activity 2023-01-12, Moderate

**Evidence label:** Moderate. **Vendor fixed?** Rank 9 closed; the email rule itself is a platform policy (fact from the issue text). Not checked further. Interpretation: real pain, but only Salesforce can fix it.

## Theme 17: Hacker News: Salesforce corporate and security news (not pain points)

**Fact:** 14 rows in the top 100 are news stories about Salesforce the company: layoffs and hiring (ranks 13, 26), acquisitions (4, 14, 18), data breaches and attacks (21, 22, 15), executive controversies (27, 47, 64, 75), and research or open source releases (31, 37). None asks for a feature or reports a product defect.

Top 3 by points (fact): rank 26 [Salesforce will hire no more software engineers in 2025](https://news.ycombinator.com/item?id=42639417), points 350, 390 comments; rank 4 [Salesforce to Acquire Fin](https://news.ycombinator.com/item?id=48540126), points 327; rank 3 is in Theme 16.

**Evidence label:** Moderate (capped). **Interpretation:** Useful as context (security incidents in 2025 and 2026 may raise demand for audit and access tooling) but not evidence of a buildable pain point.

## Theme 18: Noise to exclude next run

**Fact:** 8 rows are unrelated to Salesforce as a product. Seven are Hacker News launch posts that mention Salesforce as an integration or customer: ranks 1 (Whiteboard IDE, the top-ranked row overall), 11 (Airbyte Agents), 29 (Tokenless), 33 (Chroma Cloud), 36 (Hurston TTS), 41 (Onyx), 55 (Chert). One is GitHub housekeeping: rank 8 ([Maintainer away until April 27th](https://github.com/tprouvot/Salesforce-Inspector-reloaded/issues/1139), 18 reactions, labeled **Strong**).

**Interpretation:** Two version-2 changes would remove most of this: require a keyword in the Hacker News title, and let `config.yaml` list title patterns to drop (for example `[info]`, `OOO`, `Announcement`). Neither was in scope for version 1 and nothing was filtered by hand here.

---

## What I would look at first (interpretation)

Ranked by a mix of evidence, durability and how buildable it looks:

1. **Theme 8, metadata granularity and manifest tooling.** Eight requests, Moderate evidence, recurring 2023 to 2025, and shaped like a standalone tool.
2. **Theme 9, Inspector query and export helpers.** Admin-adjacent, half the items still open, small repo so each vote counts.
3. **Theme 13, TypeScript for LWC.** Top open LWC request by reactions, no vendor movement since 2024 in this data.
4. **Theme 12, large-volume jsforce reliability.** Two of three still open; relevant to anyone moving Salesforce data in Node.

Everything else is either fixed, vendor-only, or news. The admin pain the brief cares about most (Flow, reports, data export) is not in this run. Add `data/ideaexchange.csv` and rerun before deciding.
