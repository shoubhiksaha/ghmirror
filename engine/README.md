# ghmirror engine

Mirrors a GitHub organization (or user account) one-to-one into one or more self-managed GitLab
instances, keeps them in sync, and can promote a GitLab to be the primary if GitHub is lost.
Architecture and rationale: `../DESIGN.md`. Operations: `../docs/RUNBOOK.md`.

Normal installs use `deploy/compose/setup.sh` (Docker only), which runs `ghmirror init` for you.
By hand:

```
pip install -e '.[postgres]'          # Python 3.10+, git (git-lfs optional)
ghmirror init                         # guided: writes ghmirror.yaml, .env, identities.yaml, secrets/
ghmirror -c ghmirror.yaml check       # connectivity, admin rights, side channel
ghmirror -c ghmirror.yaml sync --full --org   # seed (idempotent, resumable)
ghmirror -c ghmirror.yaml run         # webhooks + workers + scheduler
ghmirror -c ghmirror.yaml verify      # parity report (exit 1 on drift)
ghmirror -c ghmirror.yaml promote --primary cloud   # failover;  demote = undo (drills)
```

## Modules

| Module | Role |
|---|---|
| `setup.py` + `prepare-host.sh` | `ghmirror init` (GitHub App manifest flow or token, targets, files) and the one-time GitLab host preparation |
| `config.py` | YAML config with `${ENV}` interpolation |
| `db.py` | state store on SQLite or PostgreSQL: id mapping, cursors, durable job queue (coalescing, per-repo ordering, retries, dead letters), webhook dedupe, audit log, alerts |
| `github.py` | GitHub REST/GraphQL client; token or GitHub App; rate limits; NotFound vs SourceUnavailable |
| `gitlab.py` | GitLab REST client; `Sudo` impersonation; retries incl. async-permission refresh |
| `gitmirror.py` | L1: bare mirrors, ref-exact push, deleted/rewritten tips kept under `refs/ghmirror-backup/` |
| `archive.py` | L2: every GitHub object as JSON with history; release assets and attachments as files |
| `identity.py` | mirror accounts: create/adopt, blocked group Owners, SSH/GPG keys, avatars, renames |
| `sync.py` | L3: projects, labels, milestones, issues, PRs→MRs (number parity, exact diffs, states), comments, reviews, approvals, line comments with positions/ranges/replies/resolution, reactions, releases + assets, attachments |
| `sidechannel.py` + `.rb` | persistent Rails agent for MR dates/state/merger, approvals, issue close metadata, release author, MR renumbering |
| `engine.py` | wiring, target health, deletion guard (circuit breaker), repository passes |
| `org.py` | members, teams → subgroups, team access → project shares (applied at promote) |
| `promote.py` | failover and drill reversal |
| `verify.py` | parity report and repair |
| `server.py` | webhook receiver (HMAC, dedupe), workers, scheduler, `/metrics`, `/status` |
| `migration.py` | GitHub migration archive download |
| `redirector.py` | old GitHub URLs → GitLab URLs |

## Tests

`pytest tests` (set `GHMIRROR_TEST_PG=postgresql://...` to also run the DB tests on PostgreSQL).
End-to-end against real GitHub + two GitLab CE containers: `../e2e/run_e2e.sh`.
