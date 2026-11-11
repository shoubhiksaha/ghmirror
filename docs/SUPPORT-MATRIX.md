# Supported platforms and limitations

Status words used below:

- **Tested**: exercised end to end against the real product (results in guide chapter 8).
- **Built**: implemented and unit-tested, not exercised end to end.
- **Expected**: same API as a tested platform, no known difference, not tested.
- **Unsupported**: known not to work, or missing a capability ghmirror needs.

## 1. Source: GitHub

| Edition | Status | Notes |
|---|---|---|
| GitHub.com, personal account | **Tested** | The e2e and setup tests ran against a personal account. |
| GitHub.com, organization (Free, Team) | **Built** | Organization members, teams and collaborators are implemented and unit-tested; no organization was available for the e2e. |
| GitHub Enterprise Cloud (github.com) | **Expected** | Same API as GitHub.com. Higher API limits (§4). SAML SSO: the App or token must be authorized for the organization. |
| GitHub Enterprise Cloud with Enterprise Managed Users | **Expected** | Logins carry the `_shortcode` suffix; the identity map should map them to company emails. Not tested. |
| GitHub Enterprise Cloud with data residency (`*.ghe.com`) | **Expected** | Set `source.api_url: https://api.<sub>.ghe.com` and `source.web_url: https://<sub>.ghe.com` by hand; `ghmirror init` assumes github.com. Not tested. |
| GitHub Enterprise Server 3.x | **Expected** | Set `source.api_url: https://HOST/api/v3` and `source.web_url: https://HOST` by hand. GraphQL is derived as `https://HOST/api/graphql`. The App-manifest step of `init` targets github.com; use token mode or create the App by hand. Not tested. |

**Authentication:** a GitHub App (recommended; read-only, created by `ghmirror init` in two clicks)
or a token (classic or fine-grained PAT, or a `gh` OAuth token, which is what the e2e used). The App
manifest flow was tested with a simulated GitHub, not with live clicks on github.com.

## 2. Targets: GitLab

| Edition | Status | Notes |
|---|---|---|
| GitLab CE 19.5.3, Omnibus in Docker | **Tested** | The e2e (two instances) and the setup test. Pinned in `deploy/gitlab/docker-compose.yml`. |
| GitLab CE 19.x, other patch releases | **Expected** | The engine uses long-standing REST endpoints plus the Rails models in `sidechannel.rb`. Re-run the e2e before upgrading GitLab across a minor version. |
| GitLab EE (self-managed, any tier) 19.x | **Expected** | Same code base as CE. EE adds audit events, which help with §4.4 of SECURITY.md. |
| GitLab self-managed older than 19.0 | **Not tested** | Likely to work back to 17.x, but untested and not supported until tested. |
| GitLab.com (SaaS) | **Unsupported** | No admin API, no `Sudo` impersonation, no Rails console: comments would all be by the bot, dates would be "now", MR numbers would not match PR numbers. |
| GitLab Dedicated | **Unsupported** | Admin API is available but there is no host access for the side channel. Partial operation (no side channel) would be possible but is not supported. |

**Install methods for a target:**

| How GitLab runs | Side channel | Status |
|---|---|---|
| Omnibus package on a VM or bare metal | `ssh ghmirror@host sudo gitlab-rails runner` | **Tested** in the setup test (`prepare-host.sh` Omnibus mode over real SSH). |
| Omnibus in Docker on a host | `ssh ghmirror@host sudo docker exec -i <container>` | Engine side **tested** (e2e, via local `docker exec`); `prepare-host.sh` Docker mode **built**. |
| GitLab Helm chart (Kubernetes) | `kubectl exec` into the toolbox pod | **Not built.** A custom `side_channel` command prefix is possible; untested. |
| No side channel | (none) | **Built.** Mirrors everything, but creation and merge dates become the sync time, merged PRs appear as closed with a note, and MR numbers can differ from PR numbers. |

**Engine host:** Linux with Docker Engine and Compose v2 (tested with Docker 29.5 in a Linux VM).
The engine also runs without Docker (Python 3.10+, git, optional git-lfs, PostgreSQL; SQLite for
tests); see `engine/README.md`. The Compose stack uses PostgreSQL 18 and a Python 3.15 engine image. The unit tests run on these
versions in CI. The end-to-end runs and the failover drill were recorded on Python 3.12 and
PostgreSQL 16.

## 3. What is copied

| GitHub | GitLab | Fidelity |
|---|---|---|
| Repository, description, topics, default branch, archived flag | Project | exact |
| Branches, tags, commits, git notes | Same refs | exact (force-pushes mirrored; old tips kept in the engine's L1 mirror under `refs/ghmirror-backup/`). Tested on an 86,000-commit history |
| Issues | Issues, same number | exact (author, dates, state, closer, labels, milestone, assignees, lock) |
| Pull requests | Merge requests, same number | exact (state, merger, dates, drafts, reviewers, the reviewed diff) |
| Comments, reviews, line comments, reactions | Notes, approvals, diff discussions, award emoji | exact |
| Labels, milestones, releases + assets, attachments | Same; assets in the generic package registry; attachments as uploads | exact |
| Users | Mirror accounts (blocked until promote) | exact (username, name, avatar, SSH and GPG keys) |
| Members, teams, collaborators | Group members, subgroups, shares | built (applied at promote) |
| Branch protection | Protected branches | applied at promote |
| Wikis, LFS objects | Project wiki, LFS | built (not exercised: GitHub wikis cannot be created by API for the test) |
| Discussions, Projects (boards), Actions history and logs, Packages, Pages, Codespaces, security alerts | — | **not copied** |
| Actions workflows | The `.github/workflows` files are in git; they do not run on GitLab | CI must be translated to `.gitlab-ci.yml` (process, see the runbook) |
| Secrets, deploy-key private halves, OAuth app credentials | — | never readable from GitHub; re-create them |

## 4. The offline (on-premises) target: exact limitations

"Offline" means the second GitLab runs on infrastructure you own, independent of GitHub and of
the cloud GitLab. It does **not** mean air-gapped.

1. **Not air-gapped.** The engine pushes to the offline GitLab over the network (HTTPS API, git
   over HTTPS, SSH for the side channel). The engine must reach GitHub and the offline GitLab at
   the same time. There is no export-to-media/import mode for a disconnected network.
2. **Read-only while mirroring.** People should have Reporter (read) access. Anything created by
   hand on a mirror is reported by the parity check and may be overwritten by the next sync of
   that object; it is never synced back to GitHub.
3. **Freshness depends on the engine.** With webhooks: seconds to minutes. Without a public
   webhook endpoint (common for on-prem only installs): polling every 15 minutes
   (`schedule.git`, `schedule.meta`). If the engine host is down, the copy is frozen at the last
   sync and catches up afterwards.
4. **After failover, the secondary gets git only.** When one GitLab is promoted, the other
   receives branches and tags through GitLab push mirrors (GitLab retries at most every 5
   minutes). New issues, merge requests and comments made after failover are **not** copied to the
   secondary; use GitLab project export/import if needed. A GitLab-to-GitLab metadata sync is not
   built.
5. **Links that point at GitHub.** Attachments, avatars and release assets are copied, so they
   work offline. Links written in text (for example a README image hosted on
   `raw.githubusercontent.com`, or a link to another GitHub organization) still point at GitHub.
6. **CI does not run while mirroring.** CI/CD is disabled on mirrored projects and enabled by
   `promote`. Runners, CI variables and secrets must be set up on the offline GitLab in advance.
7. **One engine, two targets.** Both GitLabs are fed by the same engine. Losing the engine host
   stops updates to both (the copies stay intact). An engine can be rebuilt from `.env`,
   `secrets/`, `ghmirror.yaml`, `identities.yaml` and the PostgreSQL database (see CAPACITY.md).
8. **Restoring from L2 is manual.** The JSON archive is evidence and a source for scripted
   restores; there is no command that replays it into an empty GitLab. The fast path to rebuild a
   lost GitLab is a fresh first copy from GitHub (`ghmirror enqueue-all`), or, if GitHub is gone,
   GitLab project export/import from the surviving GitLab.

## 5. Scale tested

| Dimension | Tested | Notes |
|---|---|---|
| Repositories | 2 per e2e run (a fixture repository, plus one created and deleted for the guard test) | Larger estates: see CAPACITY.md for the API budget per repository. |
| Issues / PRs per repository | a handful (4 issues, 4 PRs after phase 2, with comments, reviews, reactions) | Sync cost is linear in objects; the first copy dominates. |
| Git history per repository | `git/git`: 85,999 commits, 1,012 tags, 321 MiB packed; identical on GitLab, first copy 293 s (CAPACITY.md §3.1) | Git only: the history test did not copy that repository's pull requests. Repositories of several GB, and LFS, are not tested. |
| GitLab targets | 2 | Any number is supported by config; each adds one copy of all writes. |
| Concurrent workers | 2 in the e2e; 4 as written by `ghmirror init` (engine default 2) | `server.workers` in the config. |

These are functional tests plus one large-history test, not load tests. A load test on a
realistic estate (hundreds of repositories, tens of thousands of issues) has not been run.
