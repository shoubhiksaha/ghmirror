# Sample parity-verification reports

These are real reports, not mock-ups. They come from a drill on 2026-11-04 that mirrored a scratch
GitHub repository (`shoubhiksaha/ghmirror-demo-source`) into GitLab CE 19.5.3. The repository held
2 issues and 5 pull requests: merged by merge commit, by squash and by rebase, one closed and one
open. They carried 20 comments and reviews, labels, a milestone and a release.

The engine writes a JSON report for every run (`<archive>/_reports/verify-<time>.json`, used for
alerting). The Markdown rendering is for people:

```sh
ghmirror verify --format markdown --output parity.md
```

| Report | Situation | Result |
|---|---|---|
| [parity-1-pass.md](parity-1-pass.md) ([json](parity-1-pass.json)) | Right after the first copy | **PASS**, 0 differences |
| [parity-2-drift.md](parity-2-drift.md) ([json](parity-2-drift.json)) | After four changes made by hand on the GitLab mirror: issue #1 renamed, a label removed from it, a new issue opened, a new branch pushed | **DRIFT**, 4 differences: each change found, with GitHub's and GitLab's value |
| [parity-3-after-repair.md](parity-3-after-repair.md) ([json](parity-3-after-repair.json)) | After `ghmirror verify --repair` and the queued re-sync | 1 difference left, by design: the issue opened by hand exists only on GitLab, and ghmirror never deletes issues. A person decides whether to keep or delete it. The title, the label and the branch were restored from GitHub. Before the branch was removed, its commit was saved in the engine's mirror (`refs/ghmirror-backup/…/target/heads/local-experiment`). |

In production the check runs nightly (`schedule.verify`). Any difference raises an alert:
`parity check found N differences`. The measured cost for this repository was 16 GitHub API calls
and 15 seconds; [CAPACITY.md](../CAPACITY.md) scales that up.
