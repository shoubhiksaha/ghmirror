# Capacity, API limits and recovery objectives

The numbers marked **measured** come from a drill on 2026-11-04. The drill mirrored one scratch
repository (2 issues, 5 PRs, 20 comments and reviews, 3 accounts, a release) from GitHub.com into
GitLab CE 19.5.3. GitLab ran as a single container on a 4-CPU / 8 GB VM shared with the engine,
which is deliberately smaller than GitLab's recommended size. A second test the same day mirrored
the full history of the public `git/git` repository (86,000 commits, 1,012 tags; §3.1). Rows marked
**estimate** extrapolate from those numbers. Validate them with a pilot on part of your estate before committing to a
schedule (§6).

## 1. Storage

### Engine host

| Store | Size | Basis |
|---|---|---|
| L1 git mirrors (`mirrors` volume) | 1–2 × the git size of the mirrored repositories, plus LFS objects if used | bare mirrors that also keep every PR's head (`refs/pull/*`), so the commits of unmerged and closed PRs are kept too. **Measured** on `git/git`: 605 MiB against 321 MiB for a plain clone (3,410 PR refs). Old tips under `refs/ghmirror-backup/` share objects and add little |
| L2 archive (`archive` volume) | ≈ 35 KB per PR (with its reviews and comments) + 15 KB per issue + attachments and release assets at full size + one extra copy per real change | **measured**: PR JSON 32–51 KB, issue JSON 13–15 KB |
| State database (PostgreSQL) | ≈ 1–2 KB per mirrored object | **measured**: 64 KB for about 40 objects (SQLite) |
| Reports | ≈ 1 KB per nightly run plus differences | `archive/_reports/` |

A new L2 version is written only when an object really changes. Re-syncing an unchanged object
costs nothing (ROADBLOCKS E30).

**Rule of thumb for the engine host:**

```
disk ≥ 2 × git size + 1.2 × LFS size + 50 KB × (issues + PRs) + attachments + release assets + 20 GB (OS, images, headroom)
```

### Each GitLab

| Item | Size |
|---|---|
| Repositories | ≈ the GitHub git size (GitLab stores the same objects; **measured** 345 MB for `git/git`) |
| Uploads | attachments pasted into issues and comments, avatars |
| Package registry | release assets |
| Database | follows GitLab's own sizing for the number of issues, merge requests and notes |

Size each GitLab with GitLab's reference architectures for the number of *people who will use it
after a failover*, not for the mirror load alone.

### Worked example (estimate)

The example organization has:
- 300 repositories, 40 GB of git, 5 GB of LFS
- 20,000 PRs and 15,000 issues
- 10 GB of attachments and release assets

| | Engine host | Each GitLab |
|---|---|---|
| git + LFS | 86 GB | 45 GB |
| L2 JSON | 0.9 GB (+ change history) | — |
| attachments, assets | 10 GB | 10 GB |
| database | < 0.2 GB | per GitLab sizing |
| **plan for** | **≈ 120 GB** | **≈ 70 GB + GitLab baseline** |

## 2. GitHub API limits

ghmirror only reads from GitHub. Its REST budget is the token's hourly primary limit (GitHub
documentation, checked 2026-11-04):

| Authentication | Requests per hour |
|---|---|
| GitHub App installation (recommended) | 5,000 minimum; +50 per repository above 20 and +50 per organization member above 20; capped at 12,500 |
| GitHub App installation on GitHub Enterprise Cloud | 15,000 |
| Personal access token (classic or fine-grained), OAuth token | 5,000, shared with everything else that user does |

Secondary limits also apply. These include 100 concurrent requests and 900 REST points per minute,
where a read costs 1 point. With 4 workers (as written by `ghmirror init`), ghmirror stays far below both.

**How the engine behaves at the limit.** When GitHub answers 403/429 with `Retry-After` or
`X-RateLimit-Remaining: 0`, the engine sleeps until the reset and continues
(`engine/ghmirror/github.py`). Nothing fails and nothing is skipped; the copy just runs slower.
Git traffic (`git fetch`) does not count against the API limit.

### API cost per operation

| Operation | Calls | Basis |
|---|---|---|
| First copy | **measured** 32 REST calls, 0 GraphQL, for about 30 objects: ≈ 1 call per object | rate-limit counters before and after |
| Nightly parity check | **measured** 16 calls for 1 repository with 7 issues/PRs: ≈ 4 per repository + 2 per PR | idem |
| Steady state, per webhook event | 2–6 calls (the changed object and its comments) | design |
| Scheduled safety-net passes | ≈ 2 calls per repository per pass when nothing changed (listings with `since`) | estimate |

**Worked example (estimate):** for the organization in §1, with 35,000 issues and PRs:
- **First copy:** about 40,000 calls. A GitHub App with ≥ 12,500/h therefore needs about 3.5 hours
  of API budget; a 5,000/h token about 8 hours. In practice GitLab's write speed is the bottleneck (§3).
- **Nightly verify:** 300 × 4 + 20,000 × 2 ≈ 41,000 calls, which is 3.3 hours of a 12,500/h
  budget. On a 5,000/h token, run verify weekly (`schedule.verify: 604800`) or use an App.

## 3. Time for the first copy

**Measured** (1 repository, 7 issues/PRs, 20 comments, 3 accounts): **168 s** in total.

| Phase | Time | Recurrence |
|---|---|---|
| Group and project | 5 s | once per group / repository |
| Mirror accounts, with avatar and SSH/GPG keys | 5–20 s each | once per person |
| Side-channel agent start (one Rails boot on the GitLab host) | 25–60 s | once per engine start |
| Issues, merge requests, comments, timestamps, approvals | about 60 s | ≈ 2 s per issue/PR with its comments on this test GitLab |

**Estimate for the organization in §1:**
- 35,000 issues/PRs × 2 s, spread over 4 workers, takes **about 5 hours**, plus the git transfer
  of 45 GB.
- Plan for a **weekend** for the first copy. A production-sized GitLab is faster.
- The first copy is resumable: an interruption continues where it stopped.
- Until a repository's first copy is done, verify counts it as *pending*, not drift.

### 3.1 Large history (measured)

The full history of the public `git/git` repository, mirrored from GitHub.com into the test GitLab
with `ghmirror sync --no-meta` (git only; its pull requests were not copied):

| | Result |
|---|---|
| Size | 85,999 commits on 8 branches, 1,012 annotated tags, git notes (`refs/notes/amlog`), 423,000 objects, 321 MiB packed; 9 separate root commits |
| First copy, total | **293 s** on a home connection (about 2.5 MB/s from GitHub) |
| … of which pushing into GitLab | **49 s** (measured separately: the same history into an empty project) |
| GitHub API calls | **2**. Git traffic does not count against the REST limits |
| Peak memory | **744–774 MB**, in the `git` process packing the push; the engine itself stays under 30 MB |
| Catch-up pass with nothing new | **4–6 s** |
| Result | every branch, tag and notes ref has the same object ID on both sides; a fresh clone of each side gives the same 85,999 commits with identical IDs, parents, authors, committers, dates and messages; `git fsck` clean on both |

For a large repository, the first copy is limited by the download from GitHub. Pushing into GitLab
ran at about 7 MB/s on this small test GitLab. Allow **about 1 GB of engine-host memory per
worker** that may push a large repository at the same time.

## 4. Recovery point objective (RPO): how much can be lost

| Mode | RPO | Basis |
|---|---|---|
| Webhooks (public `/webhook`) | **measured** 2–5 s for a push, 8–9 s for a comment, from GitHub to GitLab | three comment probes, two push probes, signed deliveries to the running service |
| Polling only (no public endpoint; `init` without a webhook URL) | up to 15 minutes (`schedule.git`, `schedule.meta` = 900 s) | configuration |
| Engine host down when GitHub is lost | the time since the engine stopped | — |

`promote` records the last successful metadata sync of every repository (`rpo` in its output), so
the actual exposure is known per repository. Git data has an extra safety net: anyone's local
clone holds every commit they had fetched.

## 5. Recovery time objective (RTO): how long until people work again

| Step | Time | Basis |
|---|---|---|
| Decide to fail over (GitHub outage confirmed, not a blip) | your incident process; typically 15–60 min | — |
| `ghmirror promote --primary cloud --yes` | **measured** 8.3 s for 1 project and 1 account | drill recording |
| … with a reachable secondary | + 65 s once (GitLab caches the outbound allowlist) | `promote.py` |
| … for the organization in §1 (estimate) | ≈ 1 s per project + 0.5 s per account: 300 projects and 200 people take **about 7 minutes** | linear in API calls |
| People switch remotes (`git remote set-url`, or the redirector) and sign in with SSO | minutes per person, in parallel | runbook §5 |
| Secondary GitLab receives git changes from the new primary | ≤ 5 min behind (GitLab push-mirror interval) | GitLab behaviour, e2e step 10 |
| `ghmirror demote` (end of drill, or GitHub back) | **measured** 2 s | drill recording |

**Expected RTO** from the decision to developers pushing to GitLab is 15–30 minutes. Communication
dominates it; the engine's part is minutes. A twice-yearly drill (RUNBOOK §6) keeps it that way.

## 6. Pilot before committing to numbers

1. Mirror 10–20 representative repositories (`source.repos.include`).
2. Note `gh api rate_limit` before and after, the duration in `docker compose logs engine`, and
   `du -sh` of the volumes.
3. Scale the results linearly to the full estate. Size disk at 1.5× the result, and set
   `server.workers` (4 as written by init) to what the GitLab hosts sustain.
4. Run `verify --format markdown` and a promote/demote drill on the pilot group.
