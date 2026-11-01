# Prior art: open-source tools for GitHub → GitLab mirroring and backup

Researched 2026-10-31. Purpose: find what already exists, what to reuse, and
what each project teaches about the hard parts of DESIGN.md.

## Headline

No open-source project does continuous, one-to-one GitHub → GitLab sync of
pull requests, reviews and comments. The field splits into:

1. **Git-only mirrors** (many, mature) — solve layer L1.
2. **Metadata archivers** (python-github-backup, github-to-sqlite) — solve L2.
3. **One-shot importers** (GitLab's own GitHub importer, Gitea/Forgejo
   migrator) — solve the *seed* of L3 very well, but are import-once.
4. **Issue-only bridges** (git-bug) — bidirectional but no PRs/reviews.

The incremental L3 engine in DESIGN.md §5 therefore has to be built, but the
seed, the archive, the git mirror and the hardest mapping logic (diff-note
positions, user mapping, iid preservation) can be lifted from GitLab's own
importer source and from python-github-backup.

---------------------------------------------------------------------------
## Candidates

### 1. GitLab GitHub importer (gitlab-org/gitlab, `lib/gitlab/github_import`) — REUSE for seed, STUDY for engine
MIT, maintained by GitLab, ships in CE. Automatable via `POST /import/github`
(`personal_access_token`, `repo_id`, `target_namespace`, `optional_stages`
{attachments_import, collaborators_import}, `timeout_strategy`).

What the source and docs confirm (these resolve open questions in DESIGN.md):
* **MR iids are preserved**: `pull_request_importer.rb` sets `iid: pull_request.iid`
  and passes it to `create_merge_request_without_hooks`. Issues likewise.
  → Seed import guarantees number parity; §15 only needs to cover incremental PRs.
* **Timestamps preserved**: `created_at: pull_request.created_at`,
  `updated_at: pull_request.updated_at` — same in `diff_note_importer.rb`.
* **Merged PRs become merged MRs**: `state_id: MergeRequest.available_states[pull_request.state]`
  (direct DB write; the public API cannot do this, which is why our engine must
  *merge* the MR in GitLab when GitHub merges — DESIGN §6).
* **Missing fork branch**: importer creates `github/fork/{project}/{branch}` at
  `source_branch_sha` if the branch is gone. Our `gh-pr/N` fallback matches this.
* **Imports**: issues, PRs, comments, review comments, reviews, review
  suggestions, assigned reviewers, merged-by, labels, milestones, release notes,
  attachments (optional), branches + fork branches of open PRs, LFS, wiki,
  branch protection (strictest combination of GitHub+GitLab rules),
  collaborators, issue/PR events (optional).
* **Does not import**: approvals (adds a comment instead), "require status
  checks" rule, groups/organizations, custom repo roles (cause partial imports).
* **User mapping**: placeholder users + post-import reassignment ("user
  contribution mapping", GitLab 17.x+); destination user must *accept*. In
  18.0+ with enterprise users, reassignment limited to enterprise users. Not
  available when importing into a personal namespace. Lookup chain: GitHub
  user id → email → GitLab user; cached in Redis 24 h; GitHub `ghost` → GitLab
  ghost user.
* **Architecture worth copying**: 10 Sidekiq stages (repo → base data → PRs →
  collaborators → issues+diff notes → events → attachments → protected
  branches → finish); one job per PR; `AdvanceStageWorker` polls for stage
  completion; rate-limit hit → jobs rescheduled until reset; conditional
  (ETag) requests for negative user lookups don't count against quota; all
  lookups cached; `resumes_work_when_interrupted!`; stuck-import TTL refresh.
* **Diff-note position logic** (`representation/diff_note.rb`): uses GitHub
  `line`/`original_line`/`start_line`/`side` (not `position`); `side == 'RIGHT'`
  → `new_line` only, else `old_line` only; `diff_refs` from the MR; file-level
  comments use line 1 placeholder; if `diff_hunk` is missing, a default hunk is
  generated from file content at `original_commit_id`; if DiffNote creation
  fails it falls back to a `LegacyDiffNote` carrying the raw hunk, never to a
  plain note. Public API equivalent: `POST .../discussions` with
  `position[base_sha|head_sha|start_sha|old_path|new_path|old_line|new_line]`,
  `line_range` with `line_code = <sha1(path)>_<old>_<new>` for multi-line.
* **Limits**: classic PAT only (`repo`, `read:org`), 6 import starts/min/user,
  5 000 GitHub calls/h (15 000 with GHEC OAuth app), Kubernetes-size repo took
  76 h, pre-2017 PR comments import as separate threads, import is one-shot
  ("Re-import" creates a new project), and **repository mirroring does not
  sync new or updated pull requests** (docs) — confirms the gap we fill.

### 2. josegonzalez/python-github-backup — REUSE as the L2 archiver
MIT, Python 3.10+, 1.6k stars, last push 2026-10-28, maintainer calls it
feature-complete, Docker image. Flags cover: `--issues --issue-comments --issue-events --issue-timeline
--pulls --pull-comments --pull-reviews --pull-commits --pull-details --labels
--milestones --hooks --security-advisories --discussions (GraphQL) --releases
--assets --attachments --repositories --bare --wikis --lfs --gists`, `-O`
organization, `-P` private repos (not part of `--all`), `--as-app` GitHub App
tokens, `-i` incremental with per-repo/per-resource checkpoints, automatic
throttling from rate-limit headers, retries.
Lessons: it explicitly has **no restore** ("JSON metadata cannot be
faithfully recreated through the API") — exactly why L3 is a separate
engine; incremental runs can leave gaps after errors → schedule periodic
full runs (DESIGN §5 reconciler); fine-grained tokens cannot download
attachments from private repos (classic token or App needed for that).

### 3. dogsheep/github-to-sqlite — ALTERNATIVE L2 shape
Apache-2.0, Python, 475 stars, last push 2026-09. Loads repos, issues, issue comments, PRs, commits,
releases, tags, contributors, workflows into SQLite. No reviews/review
comments, incremental only for commits. Useful idea: a queryable archive
(SQLite/Postgres) beside raw JSON makes the parity report (§19) a SQL query.

### 4. ubc/github2gitlab — closest existing PR→MR tool, too thin to reuse
AGPL-3.0, Python, 46 stars, 17 commits, last push 2022-10 (effectively
unmaintained), clone of an abandoned PyPI package. Creates the
GitLab project, mirrors git, moves `refs/pull/*` → `refs/heads/pull/*`, then
creates/updates MRs matching PRs: title + description exact, state mapped;
merged PRs become *closed* MRs with a `:MERGED:` marker because the API
cannot mark an MR merged. No comments, reviews, authors, timestamps,
scheduling. Lesson: the `refs/heads/pull/N` trick lets GitLab CI run on PR
heads; the merged-state problem is real and must be solved by merging in
GitLab (same SHAs) rather than by marker text.

### 5. mao1910/github-to-gitlab-sync — org-wide git mirror pattern
MIT, Python on GitHub Actions, 3 stars, last push 2025-09 (one-person project). Discovers repos via `/user/repos` + `/users/{u}/repos`,
creates private GitLab projects, prefers GitLab pull mirroring, falls back to
`clone --mirror` + `push --mirror`; daily schedule; **prune job with grace
period and dry-run default** (`prune_state.json`, `GRACE_DAYS`). Lesson:
grace-period deletion is a good pattern but still unsafe for our failover
requirement — DESIGN §14 goes further (never delete, archive + alert, circuit
breaker).

### 6. RayLabsHQ/gitea-mirror — best-designed modern mirror UI (wrong target)
AGPL-3.0 (note the licence if code is borrowed), TypeScript/Bun/SQLite, 1.5k
stars, active (last push 2026-10-30), Docker, SSO, web UI. Mirrors public/private/starred/org
repos with code, tags, wiki, LFS, and optionally issues, PRs, labels,
milestones, releases + assets to Gitea/Forgejo (GitLab destination = code only).
PRs become enriched issues ("[PR #N]" title, status tags, commits, changed
files, comments) because Gitea's API cannot create real PRs. Four org-mapping
strategies (preserve structure / single org / flat / mixed). Scheduler checks
every minute, per-repo interval, deleted-on-GitHub repos *archived by default*,
cleanup skipped when GitHub unreachable, force-push "smart detection".
Lessons to copy: archive-not-delete default, skip-cleanup-when-source-
unreachable, per-repo last-success scheduling, operator UI with per-repo
status. Also a cautionary tale: when the destination API can't represent a PR
you degrade to an issue — GitLab's API *can* represent MRs, so we don't.

### 7. git-bug — issue bridge only
GPLv3, Go, 10.8k stars, active. Bidirectional GitHub/GitLab/Jira bridges for issues, comments,
labels, title/status changes; no PRs or reviews. Lesson: its bridge model
(import/export with per-item identity mapping stored in git) is a clean
reference for idempotent two-way mapping if we ever need GitLab→GitHub.

### 8. cooperspencer/gickup — git-only multi-host backup
Apache-2.0, Go, 1.5k stars, active (2026-10). Many sources/destinations incl. GitHub→GitLab,
local, S3; GitLab destination noted as untested by the author. No metadata.
Usable as the L1 mirror runner if we don't write our own.

### 9. GitHub Organization Migrations API (official)
`POST /orgs/{org}/migrations` with `repositories[]`, `lock_repositories`,
`exclude_attachments`, `exclude_releases`, `exclude_owner_projects`,
`exclude_git_data`, `exclude_metadata`, `org_metadata_only`; states
pending/exporting/exported/failed; archive via 302 redirect; **auto-deleted
after 7 days**; org-owner token required. Archive is the format GHE Server
imports — the most complete official dump of private+public repos, issues,
PRs, comments, releases, attachments. Confirms DESIGN §5 weekly archive job
(download within 7 days, `lock_repositories=false`).

### 10. Commercial references (feature checklists only)
Rewind, GitProtect, Cloudback, SimpleBackups: back up repos, issues, PRs,
releases, wikis, Projects v2, LFS, with restore. Their feature lists are a
useful completeness checklist; none offers GitLab as a live standby.

### Not found / dead ends
* No webhook-driven project that maps `pull_request_review_comment` to
  GitLab discussion positions.
* mendersoftware/integration-test-runner (org webhook → GitLab `pr_XXX`
  branches + pipelines) is no longer available on GitHub.
* No tool sets MR iids via the public API (only the importer's DB write).

---------------------------------------------------------------------------
## API facts verified for DESIGN.md

| Design assumption | Verified? | Source |
|---|---|---|
| Issue `iid` settable on create | Yes — "requires administrator or project owner rights" | GitLab Issues API |
| Issue/note/discussion `created_at` settable | Yes — "requires administrator or project/group owner rights"; issue `updated_at` on edit too | Issues, Notes, Discussions API |
| MR `iid` settable via API | **No** — only the importer sets it internally → §15 counter-advance or `gitlab-rails runner` stands | MR API, importer source |
| Act as another user | Yes — admin token with `sudo` scope, `Sudo: <id or username>` header; 404 if user unknown | GitLab REST authentication docs |
| Sudo as deactivated/blocked user | **Undocumented** — must be tested in the Phase 1 spike (§16) | — |
| Diff-note positions via API | Yes — `position[...]` on MR discussions; `line_range` needs `line_code` | Discussions API |
| Seed import preserves numbers + timestamps + merged state | Yes | importer source |
| Importer handles approvals | No — comment only; engine must add approvals via Approvals API | importer docs |
| Built-in mirroring syncs PRs | No | importer docs |
| Org migration archive | Yes; 7-day retention | GitHub Migrations API |

---------------------------------------------------------------------------
## What to take, concretely

1. **Seed** every project with `POST /import/github` (classic PAT, `repo`
   + `read:org`, `collaborators_import` + `attachments_import` on). Then run a
   mapping pass: match GitLab issues/MRs to GitHub by iid (guaranteed equal),
   notes by the importer's attribution footer / created_at + author.
2. **L2 archiver** = python-github-backup in a container with `-O -P
   --as-app -i --bare --lfs --wikis --issues --issue-comments --issue-events
   --pulls --pull-comments --pull-reviews --pull-details --labels --milestones
   --releases --assets --attachments --discussions`, nightly full run weekly,
   plus the weekly Migrations API archive. Load the JSON into Postgres
   (github-to-sqlite style) for parity queries.
3. **Engine** (new code) borrows from the importer: stage/queue model, per-PR
   job granularity, Redis-cached user/label/milestone lookups, ETag conditional
   requests, rate-limit reschedule, and the exact diff-note position rules
   (`side`→old/new line, file-level → line 1, hunk fallback). Writes use
   admin token + `Sudo` + `created_at`.
4. **Operational patterns** from gitea-mirror and github-to-gitlab-sync:
   per-repo last-success scheduling, archive-never-delete, skip cleanup when
   the source is unreachable, operator status UI.
5. **Branch trick** from github2gitlab: also push `refs/pull/N/head` as
   `refs/heads/gh-pr/N` only when the fork branch is gone, so CI and MRs have
   a real source branch.
6. **Known no-gos** to keep in the limits list: MR iid via API, approvals via
   importer, status-check rules, Sudo-as-deactivated (pending test).

## Sources
- https://docs.gitlab.com/user/project/import/github/
- https://docs.gitlab.com/development/github_importer/
- https://docs.gitlab.com/api/import/
- https://gitlab.com/gitlab-org/gitlab/-/blob/master/lib/gitlab/github_import/importer/pull_request_importer.rb
- https://gitlab.com/gitlab-org/gitlab/-/blob/master/lib/gitlab/github_import/importer/diff_note_importer.rb
- https://gitlab.com/gitlab-org/gitlab/-/blob/master/lib/gitlab/github_import/representation/diff_note.rb
- https://docs.gitlab.com/api/issues/ , https://docs.gitlab.com/api/notes/ , https://docs.gitlab.com/api/discussions/ , https://docs.gitlab.com/api/rest/authentication/
- https://docs.gitlab.com/development/user_contribution_mapping/
- https://docs.github.com/en/rest/migrations/orgs
- https://github.com/josegonzalez/python-github-backup
- https://github.com/dogsheep/github-to-sqlite
- https://github.com/ubc/github2gitlab
- https://github.com/mao1910/github-to-gitlab-sync
- https://github.com/RayLabsHQ/gitea-mirror
- https://github.com/git-bug/git-bug/blob/master/doc/usage/third-party.md
- https://github.com/cooperspencer/gickup
