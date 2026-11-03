# ghmirror runbook

Operating the GitHub → GitLab (cloud + offline) mirror described in `DESIGN.md`.
Engine code: `engine/`. Deployment: `deploy/`. End-to-end test: `e2e/`.

## 1. One-time setup

Each GitLab target is a self-managed CE/EE on its own host with GitLab's recommended 8 GB RAM /
4 cores plus ~1 GB for the side-channel agent (two instances on one 8 GB box overload:
ROADBLOCKS E14). `deploy/gitlab/docker-compose.yml` is a ready template. Then, on the engine host:

    cd deploy/compose && ./setup.sh

`setup.sh` (Docker only) builds the engine image and runs `ghmirror init`, which:
- creates the side-channel SSH key and prints the one command that prepares each GitLab host
  (`prepare-host.sh`: Unix user `ghmirror` reachable only with that key, an exact sudo rule for
  `gitlab-rails`/`sh`/`pkill` or `docker exec -i <container>`, the admin bot `github-mirror` and
  its token). The host prints a `ghmirror-target:` line that is pasted back into init;
- creates a read-only GitHub App through GitHub's manifest flow (two clicks: Create, Install) or
  takes a classic PAT (`repo, read:org`; `admin:org_hook` to also create the org webhook);
- writes `ghmirror.yaml`, `.env` (0600), `identities.yaml` (members pre-listed), `secrets/`.

It then runs `ghmirror check`, starts PostgreSQL + the engine and queues the first full copy.
Re-running keeps existing secrets and targets; every question has a flag for unattended runs
(`ghmirror init --help`).

Before a failover can be needed: configure company SSO (OIDC/SAML) with `omniauth_auto_link_user`
so the first login links each person to their mirror account, and fill in `identities.yaml`.
Optional: a classic PAT of an org owner (`repo`, `admin:org`) for the weekly migration archive.

Without the side channel the engine still runs but cannot set MR creation dates, squash-merged
state or approval dates (they become notes instead).

## 2. First copy

Queued by `setup.sh`; by hand: `ghmirror enqueue-all` (service) or `ghmirror sync --full --org`.
Idempotent and resumable: re-running after an interruption continues where it stopped.
Large organizations: narrow `source.repos.include` first, widen in steps.
Expect about 1 API call per object; a GitHub App installation gets at least 5,000/h.
Until a repository's first copy is done, `verify` counts it as `pending`, not drift (1 day grace).

## 3. Steady state

`ghmirror run` (the compose service) = webhook receiver + workers + scheduler.

| Signal | Where | Alert when |
|---|---|---|
| sync lag | `/metrics` `ghmirror_meta_age_seconds` | > 2 × `schedule.meta` |
| dead jobs | `ghmirror_jobs{status="dead"}` | > 0 |
| parity drift | `ghmirror_drift_count` (nightly `verify --repair`) | > 0 |
| guard halt | `ghmirror_halted` | = 1 (pages a human) |

`ghmirror status` prints mode, queue, last parity report and recent alerts.

## 4. Deletion guard (halt)

The engine halts all writes when the GitHub owner returns 404, or when more repos/issues
disappear in one pass than `guard.max_disappear_*` allows. A single deleted repository is
archived on GitLab (never deleted). On halt:

1. Check GitHub status / the organization page / the App installation.
2. GitHub fine (e.g. a mass transfer you made on purpose) → `ghmirror guard reset`.
3. GitHub gone or hostile → do **not** reset; go to section 5.

## 5. Failover (GitHub lost)

    ghmirror promote --primary cloud      # or offline

What it does, in order: stops the GitHub source; replaces the mirror's bot-only branch rule
with the real GitHub branch protections from the archive; enables CI; unblocks every human
mirror account (bots and ex-members stay blocked), applies company emails from
`identities.yaml`, drops the temporary Owner role and applies real memberships (org admins →
Owner, members → base permission, teams → subgroups + project shares, collaborators → project
members); posts a banner; turns the other target into a push-mirrored secondary (the
secondary's `mirror_url` host is added to the primary's outbound allowlist when it is on a
private network, since GitLab refuses local-network mirrors by default).

The secondary trails the primary by up to 5 minutes: GitLab runs a push mirror of all
branches at most once per 5 minutes (ROADBLOCKS E15).

Every change is written to the promotion record as it is made. A step that fails (one
project, one account, one mirror) is listed under `failures`, alerted, and the rest
continues; the exit code is then 1. Fix the cause, then `ghmirror demote` + `ghmirror
promote` again: demote undoes exactly what the record lists, even after a crash midway.

Then:
- `docker compose --profile failover up -d redirector` and point `github.com` bookmarks/
  docs links at it (or your vanity DNS at the promoted GitLab).
- Tell developers: `git remote set-url origin git@git.company.com:<org>/<repo>.git`
  (or push the `url.<new>.insteadOf <old>` git config via MDM). Same SSH keys work.
- First SSO login links each person to their account. Without SSO: `Admin > Users > reset
  password` per person.
- RPO is printed by promote (`rpo`: last successful sync per repo).

## 6. Drills (monthly)

Run on a snapshot of the offline instance or a scratch GitLab, never on the live cloud mirror:
`promote` → two engineers clone, branch, MR, approve, merge, tag → `demote` → delete the merge
requests and issues made during the drill → `sync --full` → `verify --format markdown` must report
**PASS**. `e2e/run_e2e.sh` steps 11–13 automate exactly this; `docs/demo/` is a recording of one drill.

Why the cleanup matters:
- Right after `demote`, `verify` reports the drill's work (branches moved, merge requests that
  exist only on GitLab). ghmirror never deletes those itself.
- The next sync resets the branches to GitHub. Any commit that exists only on GitLab is first saved
  in the engine's git mirror under `refs/ghmirror-backup/<time>/target/`.
- Merge requests and issues stay until a person deletes them. A drill MR left in place (say !8)
  also takes the number that GitHub's next PR #8 needs, which breaks number parity for that PR.
  This is why drills belong on a snapshot.

## 7. Restore drill (quarterly)

Rebuilding a lost GitLab, by situation:
- **GitHub available**: new empty GitLab, `prepare-host.sh`, `./setup.sh` (add it as a target),
  and the first copy rebuilds it exactly like the original seed.
- **GitHub gone, other GitLab alive**: move the projects from the surviving GitLab with GitLab's
  own group/project export + import (or Direct Transfer); this keeps issues, MRs and notes.
- **Code only, from the engine host**: `git push --mirror` from the L1 bare mirrors under
  `/data/mirrors`. The L2 archive (`/data/archive`, plus the weekly `_migrations/*.tar.gz`)
  preserves all metadata as JSON for reference; replaying it into GitLab is not automated.

## 8. Known limits

See DESIGN.md §13, ROADBLOCKS.md and docs/SUPPORT-MATRIX.md. In short: GitHub Discussions,
Projects, Actions run history and secret values are not mirrored or archived; MR creation dates,
squash-merged state and approval dates need the side channel; extra branches named
`gh-pr/<n>` exist only for PRs from forks that are still open; the L2 archive is not replayed
into GitLab automatically (section 7).
