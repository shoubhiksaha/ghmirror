# Parity report: GitHub `shoubhiksaha` vs GitLab

| | |
|---|---|
| Generated | 2026-11-03 22:41:28 UTC |
| Targets | cloud |
| Result | **DRIFT**: 4 difference(s) |
| Repair requested | no |

## Coverage

| Repositories | Issues compared | Pull requests / merge requests compared | Pending first copy |
|---:|---:|---:|---:|
| 1 | 2 | 5 | 0 |

Each repository is compared on every branch and tag (commit SHA), every issue (title, state, author,
creation time, labels, milestone, comment count), every pull request (the same, plus target branch,
merged-by and merge time), and the sets of labels, milestones and releases. Counts are per target.

Repositories: `shoubhiksaha/ghmirror-demo-source`

## Differences

| Target | Repository | Object | Field | GitHub | GitLab |
|---|---|---|---|---|---|
| cloud | `shoubhiksaha/ghmirror-demo-source` | `refs/heads/local-experiment` | sha (branch or tag points at a different commit) | *(none)* | `8c66c426b0fa4c4d34c34be1d9473e3bf2b2ffd5` |
| cloud | `shoubhiksaha/ghmirror-demo-source` | issue #1 | title | `Crash on startup` | `Crash on startup (edited on the mirror)` |
| cloud | `shoubhiksaha/ghmirror-demo-source` | issue #1 | labels | `["bug", "mirror-test"]` | `["mirror-test"]` |
| cloud | `shoubhiksaha/ghmirror-demo-source` | issue #3 | exists (present on one side only) | `absent` | `present` |

GitHub is the source of truth. `ghmirror verify --repair` marks the drifted issues and merge
requests as changed and queues a full re-sync of the affected repositories, which rewrites them
from GitHub. Issues and merge requests that exist only on GitLab (made by hand on a mirror, or left
over from a drill) are never deleted: they stay reported for a person to decide. Branches and tags
are made identical to GitHub; a commit that exists only on GitLab is first saved in the engine's
git mirror under `refs/ghmirror-backup/<time>/target/`, so no work is lost.

