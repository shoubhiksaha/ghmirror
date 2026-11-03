# GitHub → GitLab (Cloud + Offline) One-to-One Mirror — System Design

Goal: every repository, branch, tag, pull request, review, comment, issue, label,
milestone, release, wiki, team, member and permission in a GitHub organization is
reproduced automatically in two GitLab instances (one in the cloud, one on-prem /
offline), with authorship and timestamps preserved as faithfully as GitLab allows,
plus a lossless raw archive so nothing is ever lost even where GitLab has no
equivalent object. Works for private and public repositories alike.

---------------------------------------------------------------------------
## 1. The three layers of "a copy"

Exactness differs by layer. Design for all three; don't pretend layer 3 is lossless.

| Layer | What | Fidelity | Mechanism |
|---|---|---|---|
| L1 Git data | All refs, commits, tags, LFS objects, wiki repos | Lossless, bit-identical | `git fetch --prune` into a bare mirror, `git push --mirror` to each GitLab |
| L2 Raw archive | Every JSON object GitHub exposes: issues, PRs, reviews, review threads, comments, reactions, labels, milestones, releases + assets, Discussions, Projects, Actions run logs, branch protection, webhooks config, members, teams, permissions, packages | Lossless (it *is* GitHub's data) | REST + GraphQL dumps keyed by node_id, append-only on object storage + local disk; weekly org-level Migration Archive |
| L3 GitLab objects | Issues → issues, PRs → MRs, review comments → MR diff notes, labels, milestones, releases, wiki, groups/members/roles | Near-lossless on self-managed GitLab; authorship lossy on GitLab.com SaaS | Sync engine (webhooks + reconciler) using the GitLab API |

Anything GitHub has and GitLab cannot represent (Discussions, Projects v2,
Codespaces, Dependabot alerts, Actions run history) was meant to live in L2 only.
**As built, L2 does not capture these yet** (§22): the engine archives issues, pull
requests, reviews, comments, reactions, labels, milestones, releases and their assets.

---------------------------------------------------------------------------
## 2. Topology

```
                 GitHub Organization (private + public repos)
                            │
            GitHub App (org-installed) ── webhooks + API
                            │
                ┌───────────▼───────────┐
                │      SYNC ENGINE       │   (containers; stateless workers + Postgres)
                │  webhook receiver      │
                │  queue  → workers      │
                │  reconciler (cron)     │
                │  mapping DB            │
                │  L2 archiver           │
                └───┬───────────────┬────┘
                    │               │
        ┌───────────▼──┐      ┌─────▼──────────────┐
        │ GitLab CLOUD │      │ GitLab OFFLINE/LAN │
        │ (self-managed│      │ (self-managed EE/CE│
        │  on a VM/K8s,│      │  on-prem, outbound-│
        │  or GitLab.com)│    │  only network)     │
        └──────────────┘      └────────────────────┘
                    │               │
              Object storage   Local disk / NAS  ← L2 archive + bare mirrors
```

Deployment options for the engine:

* **Single engine, two targets (recommended):** the engine runs on-prem next to
  the offline GitLab. It only needs *outbound* HTTPS to GitHub and to the cloud
  GitLab; no inbound ports. Webhooks reach it via a relay (Cloudflare Tunnel,
  Tailscale Funnel, smee.io-style relay, or a tiny cloud receiver that drops
  events into a queue the engine polls). If the relay is down, the reconciler
  catches up — webhooks are an accelerator, never the source of truth.
* **Two engines, one per target:** both pull from GitHub independently. More
  API quota used, but no cross-site dependency. Use if the two sites must not
  trust each other.
* **Truly air-gapped target:** engine on the connected side writes `git bundle`
  files + L2 JSON deltas to removable media / a data diode; an import-only
  engine on the air-gapped side applies them. Same code path, different transport.

---------------------------------------------------------------------------
## 3. Authentication (private repos included)

**Source (GitHub):** install a **GitHub App** on the organization (not a personal
token). Reasons: not tied to an employee, fine-grained permissions, 5 000
req/hr per installation (15 000 on Enterprise Cloud), short-lived installation
tokens, built-in webhooks with HMAC signatures. Required permissions, all
read-only: Contents, Metadata, Issues, Pull requests, Discussions, Projects,
Actions, Administration (branch protection), Webhooks, Members, Organization
administration. Private repos need nothing extra — the App's access *is* the
grant. Fallback: an org-owned fine-grained PAT with `repo`, `read:org`,
`read:discussion`, `read:packages`.

**Targets (GitLab):** a dedicated `github-mirror` bot user per instance with a
group-level access token (Owner role). On **self-managed** instances also give
the bot **admin** so it can (a) impersonate users via the `Sudo` header /
impersonation tokens and (b) set `created_at` on issues, MRs and notes. That is
what makes authorship and timestamps exact.

Secrets live in a secrets manager (Vault / SOPS / cloud KMS); the engine mounts
them at runtime; nothing in the repo.

Visibility is mirrored: private GitHub repo → private GitLab project in a
private group; internal → internal; public → public (or private, by policy).

---------------------------------------------------------------------------
## 4. Identity: mirror accounts

GitHub login → GitLab user. One mapping table, used by both targets.

**Self-managed GitLab (cloud VM and offline):**
* Engine creates one GitLab user per GitHub account seen in the org: same
  username, display name, avatar; email = the user's verified GitHub email if
  the App can read it, otherwise `<login>@github.mirror.<yourdomain>`.
* Accounts are created *blocked / no-login* (ghost accounts) unless the real
  person later claims them. Blocked users can still be the author of imported
  content when created via admin + `Sudo`.
* Teams → GitLab groups/subgroups; GitHub role → GitLab role
  (read→Reporter, triage→Reporter, write→Developer, maintain→Maintainer,
  admin→Owner). Outside collaborators → direct project members.
* Result: every note, issue, MR shows the real author and original timestamp.

**GitLab.com SaaS (if you insist on SaaS for the cloud copy):**
* You cannot create accounts for other people or impersonate them.
* GitLab's built-in GitHub importer does user mapping (placeholder users +
  reassignment, GitLab 17.x "user contribution mapping") at import time only.
* For ongoing sync the bot posts as itself with a header line
  `**@octocat** commented on 2025-03-01 14:02 UTC (github)` — correct content,
  wrong avatar/author field.
* **Recommendation:** make the cloud copy a *self-managed* GitLab (CE is free)
  on a VM/Kubernetes, not GitLab.com. Both targets then behave identically and
  the engine has one code path.

---------------------------------------------------------------------------
## 5. Sync engine — components

1. **Webhook receiver** – verifies `X-Hub-Signature-256`, dedupes by
   `X-GitHub-Delivery`, enqueues `{event, repo, node_id}`. Returns 200 fast.
2. **Queue** – per-repository ordering key so events for one repo are applied
   in order. *As built:* a durable table in the state DB (PostgreSQL
   `FOR UPDATE SKIP LOCKED`, or SQLite), with coalescing of identical pending
   jobs, retries with backoff and dead letters; no Redis needed.
3. **Workers** – pull an event, fetch the *current* object from GitHub (never
   trust the payload alone), upsert into L2 archive, then upsert into each
   target via the mapping DB. Idempotent: every write looks up
   `(target, github_node_id) → gitlab_id` first.
4. **Mapping DB (Postgres)** – tables: `repos`, `users`, `issues`, `merge_requests`,
   `notes`, `labels`, `milestones`, `releases`, `sync_cursors`, `dead_letter`.
   Each row stores GitHub `node_id`, GitHub `updated_at`, GitLab id per target,
   content hash, last_synced_at.
5. **Reconciler (cron)** – the real guarantee of completeness:
   * every 15 min: `git fetch --prune` + `push --mirror` for repos with recent
     pushes; every 6 h for all repos;
   * every hour: list issues/PRs with `since=<cursor>` per repo, diff against DB,
     repair; walk comments/reviews of anything whose `updated_at` moved;
   * nightly: full ref-hash comparison (`git ls-remote` GitHub vs each GitLab),
     object counts (issues, MRs, notes, labels) compared, drift → alert + repair;
   * weekly: GitHub **Organization Migration Archive** (`POST /orgs/{org}/migrations`,
     includes private repos, all metadata, attachments; needs an org-owner
     classic PAT with `repo` + `admin:org`, archive < 20 GB, auto-deleted after
     7 days so download immediately) stored to object storage.
     This is the "nuclear" backup independent of the engine's own logic.
6. **L2 archiver** – writes `archive/<repo>/<type>/<node_id>.json` (and binary
   assets: release assets, issue attachments, Actions logs) to local disk and
   object storage (S3/MinIO/Backblaze) with versioning + object lock.
7. **Verifier / alerting** – Prometheus metrics (lag seconds, dead-letter count,
   drift count), alerts to Slack/email, daily digest.
8. **Side-channel adapter (self-managed only)** – a tiny agent on each GitLab
   host, used only for the few fields the public API cannot set: MR
   `created_at`/`updated_at`, merged/closed state + `merged_by`/`merged_at`/
   `closed_at`, the real target branch of MRs created against a temporary
   base, approval timestamps, issue `closed_at`/`closed_by`, release author,
   and MR renumbering when number parity was lost. *As built:* the engine
   installs `sidechannel.rb` over the configured command prefix (`ssh host
   sudo` or `docker exec -i gitlab`) and keeps one `gitlab-rails runner`
   process per target alive, streaming JSON batches (Rails boots once).
   Every operation is idempotent and written to the audit log.

---------------------------------------------------------------------------
## 6. Object mapping rules (L3)

| GitHub | GitLab | Notes |
|---|---|---|
| Organization | Top-level group | name, avatar, description |
| Team / nested team | Subgroup | membership + role mapped |
| Repository | Project | visibility, description, topics, default branch, archived flag, fork relationship (note only) |
| Branches, tags | Same | mirror bot allowed to force-push; protected-branch rules re-created *after* mirror, from GitHub branch protection |
| Issue #N | Issue iid N | API allows setting `iid` (owner/admin). Body gets a footer linking the GitHub URL. |
| PR #N | MR iid N | iid parity is mandatory (see §15). Open same-repo PRs use the real head branch, so work continues on it after failover. Fork PRs and already merged/closed PRs use a helper branch `gh-pr/N` at `refs/pull/N/head`. Merged/closed PRs are created against a temporary `gh-pr-base/N` at the PR's base SHA, so GitLab stores the PR's exact diff. The side channel then restores the real target branch and state, and both helper branches are deleted; the commits stay reachable through hidden refs `refs/ghmirror/pull/N/*`. |
| PR state merged | MR merged | Merge-commit, rebase and fast-forward merges: GitLab marks the MR merged by itself once the mirrored target branch contains the MR head (verified). Squash merges: the head is not reachable, so the engine marks the MR merged through the side channel (§5 item 8) with GitHub's `merged_by`/`merged_at`. Closed-unmerged → close. |
| Review (approve/request changes/comment) | MR note + approval | Approvals via MR approvals API as the mapped user (self-managed). |
| Review comment on a line | MR diff discussion (`position`) | Needs base/head/start SHAs — engine has them from the PR object. Falls back to plain note quoting the diff hunk if position is unresolvable. |
| Issue/PR comment | Note | author + created_at via Sudo + admin |
| Reactions | Award emoji | mapped names (👍 thumbsup, etc.) |
| Labels / milestones | Labels / milestones | color, description, due date |
| Release + assets | Release + asset links | assets uploaded to GitLab generic package registry or project uploads |
| Wiki | Wiki | plain git mirror of `<repo>.wiki.git` |
| Discussions | *none* → L2 + optional issue with label `github-discussion` | |
| Projects v2 | *none* → L2 (+ optional issue board labels) | |
| Actions workflows | files already in repo; run history/logs → L2 | GitLab CI translation is out of scope |
| Packages (GHCR, npm, maven) | GitLab registry | `skopeo sync` for containers; others via package APIs |
| Branch protection, CODEOWNERS | Protected branches, approval rules | best effort |
| Webhooks, secrets | archive config only; secret *values* are not readable | |

---------------------------------------------------------------------------
## 7. Initial seed vs. steady state

(Prior-art survey and verified API facts: see RESEARCH-prior-art.md.)

**Seed, as built:** the engine's own idempotent sync (`ghmirror sync --full`)
is the default seed. It writes straight into the ghost accounts, so there are no
placeholders to reassign, and it is resumable. Every object is adopted if it
already exists in GitLab (issues/MRs by number, labels/milestones by name, notes
by author + second + body), so a project seeded by GitLab's importer, or a lost
state DB, is taken over instead of duplicated. The importer remains an option
for very large repositories:

**Seed via importer (alternative):** use GitLab's built-in **GitHub importer**
(`POST /import/github`, classic PAT of a service account with `repo` +
`read:org`; enable `import_sources[]=github` on self-managed first). Verified
end to end (ROADBLOCKS.md §C): iids, timestamps, merged state and merged_by,
review threads as diff notes, labels, tags, branches all preserved; one
placeholder user per GitHub identity. Then: set the instance setting
`allow_bypass_placeholder_confirmation=true`, download the placeholder CSV
(`GET /groups/:id/placeholder_reassignments`), fill the GitLab username column
from the identity map (§16) and upload it — contributions move to the ghost
accounts without any user action. Finally the engine's reconciliation pass
records GitHub↔GitLab ids in the mapping DB (issues/MRs by iid, notes by
author + created_at), adds approvals the importer cannot (A13) and re-applies
the mirror-bot branch-protection rule the importer overwrote.

**Steady state:** webhooks + reconciler as in §5. Never re-run the importer on
a project that exists — it is import-once by design.

**New repo created on GitHub:** `repository.created` event → engine creates the
GitLab project, runs the importer for it, registers mappings, done.

---------------------------------------------------------------------------
## 8. Git mirroring details

* Store a **bare mirror** per repo on the engine host (`/mirrors/<org>/<repo>.git`)
  — this is itself a complete L1 backup and makes pushes to both targets cheap.
* `git fetch --prune --prune-tags` then `git lfs fetch --all`, then
  `git push --mirror` … *but* GitLab rejects or hides `refs/pull/*`; push
  `refs/heads/*` + `refs/tags/*` explicitly and keep `refs/pull/*` only in the
  bare mirror. `git lfs push --all` afterwards.
* GitLab **pull mirroring** (built-in, Premium tier) can replace this step on
  paid instances, but it polls every ~30 min, drops LFS edge cases and gives
  you no bare copy — keep the engine-driven mirror even if you also enable it.
* Large monorepos: use `--filter=blob:none` first fetch, then unfilter; schedule.
* Force-pushes and deletions on GitHub are mirrored (that is "exactly the
  same"); the L1 bare mirror keeps reflogs with `core.logAllRefUpdates=true`
  and `gc.reflogExpire=never` so deleted history is still recoverable, and
  nightly `git bundle --all` snapshots go to object storage with retention.

---------------------------------------------------------------------------
## 9. Failure handling & guarantees

* **At-least-once + idempotent** = effectively exactly-once at the object level.
* Dead-letter after 5 retries; daily report; reconciler will retry anyway.
* Rate limits: honour `Retry-After`/`X-RateLimit-Reset`; GraphQL for bulk reads
  (one call per PR gets reviews + threads + comments); conditional requests
  (ETag) for polling.
* Engine outage of any length: reconciler cursors are persisted; catch-up is
  automatic. Webhook deliveries are also redeliverable from the App for 3 days.
* Target outage: queue backs up per target; other target continues.
* Verification is a first-class job: ref hashes, counts, and sampled
  content hashes must match or an alert fires. Quarterly **restore drill**:
  stand up a fresh GitLab from the L1 bundles + L2 archive and diff.

---------------------------------------------------------------------------
## 10. Security

* Engine has read-only on GitHub; write only to GitLab mirror groups.
* GitLab mirror groups are private; mirror bot is the only writer; real users
  get read access so the mirror cannot diverge via manual edits (or mark
  mirrored projects read-only except the bot).
* Webhook HMAC verification; TLS everywhere; secrets in a vault; audit log of
  every write the engine makes (it already has the mapping table).
* Offline instance: no inbound connections; outbound allow-list = api.github.com,
  github.com, cloud GitLab, object storage.

---------------------------------------------------------------------------
## 11. Implementation stack (suggested)

* Engine: Python 3.12 (PyGithub / gql + python-gitlab) **or** Go (go-github,
  go-gitlab). Python is faster to build; Go for a single static binary.
* Postgres 16, Redis 7, MinIO (local) + S3-compatible cloud bucket.
* Packaged as `docker compose` (small org) or Helm chart (K8s).
* GitLab targets: GitLab CE/EE via official Docker image or Omnibus on a VM;
  offline target can be the same compose file with GitLab + engine + MinIO.
* Existing pieces to reuse instead of rewriting: GitLab GitHub importer (seed),
  `github-backup` / `gh-migrate` style tools for L2 ideas, `skopeo` for registries.

Repo layout for this project:

```
/engine            webhook receiver, workers, reconciler, archiver (one service, many commands)
/deploy/compose    docker-compose for engine + postgres + redis + minio
/deploy/gitlab     compose/Helm values for the two GitLab instances
/mapping           role/label/emoji mapping YAML
/docs              this design, runbooks, restore drill
```

---------------------------------------------------------------------------
## 12. Phased delivery

| Phase | Deliverable | Value |
|---|---|---|
| 0 | Inventory script: repos (incl. private), sizes, LFS, users, teams, API object counts; pick GitLab flavour for cloud copy | Sizing + decisions |
| 1 | GitHub App + engine skeleton; L1 bare mirrors + push to both GitLabs; L2 archiver; weekly org migration archive | A *real* lossless backup in days |
| 2 | Seed both GitLabs with the built-in importer; mapping DB populated | Browsable MRs/issues with history |
| 3 | Incremental sync: issues, PRs/MRs, comments, reviews, labels, milestones, releases; webhooks + reconciler | Live one-to-one |
| 4 | Identity & access: ghost accounts, groups/subgroups, roles, protected branches | Org structure mirrored |
| 5 | Verification, alerting, dashboards, restore drill, runbooks | Trust |

---------------------------------------------------------------------------
## 13. Known limits (state them up front)

(Every limit below was tested or source-verified; see ROADBLOCKS.md.)

* MR iids cannot be set directly via the API; parity is kept by the counter
  advance technique or `gitlab-rails runner` on self-managed (§15). This is one
  more reason the targets must be self-managed.
* GitLab.com SaaS cannot show true authorship for ongoing sync → use
  self-managed for exactness.
* GitHub Discussions, Projects v2, Actions run history, Codespaces, security
  alerts have no GitLab equivalent and are not captured (not in L2 either; §22).
* Secret values, deploy keys' private halves, and OAuth app credentials are
  never readable and therefore never mirrored.
* While GitHub is the source, GitLab copies are read-only mirrors; that is what
  makes "exactly the same" achievable. After promotion (§14) GitLab becomes the
  source and the engine's source adapter switches to GitLab → GitLab so the
  second copy keeps mirroring. Live two-way sync is deliberately not built.

===========================================================================
# PART B — BUSINESS CONTINUITY: GitLab AS HOT STANDBY

Requirement: if GitHub suspends or deletes the organization, the team switches
to GitLab and keeps working with minimal change to their flow. The only
intended manual step is turning mirror accounts into the users' real accounts.

---------------------------------------------------------------------------
## 14. Operating modes and the promote switch

The engine and both GitLab instances are always in one of two modes.

**MIRROR mode (normal):**
* GitLab projects are write-locked for humans: every branch protected with
  "allowed to push/merge = mirror bot only"; MR merge performed only by the
  engine; group-level push rule rejects non-bot pushes.
* Mirror accounts exist but cannot sign in (see §16).
* CI pipelines on mirrored projects are disabled except scheduled **shadow
  pipelines** that prove the translated CI still passes (§17).
* Engine source adapter = GitHub; targets = cloud GitLab + offline GitLab.

**PRIMARY mode (after failover):** one command, `engine promote --primary cloud`
(or `offline`), executed by an admin, does in order:
1. Stop the GitHub source adapter; freeze last-known-good cursors.
2. Replace bot-only branch protection with the **real** rules copied from GitHub
   branch protection / rulesets (stored in L2 since the last sync): required
   approvals, code-owner approval, required pipelines, force-push rules.
3. Enable pipelines, merge trains/approvals, container & package registries.
4. Activate user accounts and send claim invitations (§16).
5. Re-point the vanity DNS name (§18) at the promoted instance.
6. Switch the engine to source = promoted GitLab, target = the other GitLab, so
   the second copy continues as a mirror (GitLab→GitLab adapter + GitLab's own
   free push-mirroring for git data).
7. Post a status banner (GitLab broadcast message) with the runbook link.

Target RTO: under 1 hour from decision to first merged MR. RPO: last successful
sync, normally minutes; the reconciler timestamps make the exact gap knowable.

`engine demote` reverses it for drills (and if GitHub ever comes back and you
decide to return, which would require a GitLab→GitHub export — a documented
procedure, not live sync).

**Deletion-propagation guard (critical):** a source that is being deleted looks
like "every object vanished". The engine therefore:
* never deletes a GitLab project, user or group on its own — repository deletion
  on GitHub archives the GitLab project and raises an alert;
* has a circuit breaker: if more than 2% of tracked objects disappear in one
  reconciliation pass, or the org/installation returns 404/suspended, it halts
  all writes, keeps the last-known-good state and pages a human;
* promotion is a human decision; the engine only recommends it.

---------------------------------------------------------------------------
## 15. Number parity: issue #N and PR #N must stay #N

Minimal flow change requires that `#123`, `org/repo#123`, commit messages,
changelogs, Slack links and docs keep resolving. Issues and MRs have separate
iid sequences in GitLab while GitHub shares one, so each GitLab sequence will
have gaps — that is fine, but the numbers must match.

* **Issues:** `POST /projects/:id/issues` accepts `iid` for owner/admin. Done.
* **Merge requests:** the API does not accept `iid`, but GitLab's iid counter
  is monotonic per project. Before creating MR for PR #N the engine checks the
  project's next MR iid; if it is below N, it advances the counter by creating
  and immediately deleting throwaway MRs (API-only, deterministic, cheap), then
  creates the real MR which lands on iid N. On self-managed the alternative is
  a `gitlab-rails runner` one-liner that sets `merge_requests.iid`; the engine
  supports both, defaulting to the API method. The seed import already
  preserves iids, so this only affects incremental PRs.
* If parity is lost anyway (e.g. PR N failed to create while N+1 succeeded),
  later MR creations in that pass are deferred and the side channel moves the
  MR to iid N (`mr_iid`, also rewriting `refs/merge-requests/N/head`). Tested.
* Verification job: nightly `SELECT` of (issue iid, MR iid) vs GitHub numbers;
  any mismatch is a hard alert.
* Result: a GitHub URL `github.com/org/repo/pull/123` maps 1:1 to
  `git.company.com/org/repo/-/merge_requests/123`. A tiny redirector service
  rewrites old GitHub URLs to the new host so bookmarks and docs keep working.

---------------------------------------------------------------------------
## 16. Accounts: from mirror identity to real identity in one step

Design the mirror accounts so that "making them real" is a flip, not a migration.

**During MIRROR mode the engine keeps each ghost account current with:**
* username = GitHub login (GitLab reserves a few names; mapping table handles
  collisions), display name, avatar, bio, company;
* primary email = the user's org-verified GitHub email (readable by the App via
  the org members / SAML identity endpoints), plus any public emails as
  secondary — this is the key that lets them claim the account;
* **SSH public keys and GPG keys** pulled from GitHub (`/users/{login}/keys`,
  `/users/{login}/gpg_keys`). On day one every developer's existing key works
  and historic signed commits show "Verified";
* group/project memberships with the real mapped roles (read→Reporter,
  triage→Reporter, write→Developer, maintain→Maintainer, admin→Owner);
  the write-lock in §14 is what keeps the mirror clean, not reduced roles;
* an `external_identity` row (GitHub user id) so the mapping survives renames.

Account state in MIRROR mode (verified on GitLab CE 19.5, see ROADBLOCKS.md):
ghost accounts are created active, then immediately **blocked**, and are made
**Owners of the top-level mirror group**. Blocked users cannot sign in, push,
or call the API themselves, yet the admin bot can act as them with the `Sudo`
header, and because they hold Owner (inherited through the group) GitLab
honours `created_at` and `iid` on everything written in their name. Ownership
is harmless while blocked; real roles replace it at promote (§14 step 2/4).
(Deactivated users can sign in and auto-reactivate, so deactivation is not a
lock; and `created_at` is silently ignored for non-Owners.)

**Promotion flips them to real accounts (step 4 of promote):**
* Preferred: company SSO (SAML or OIDC against Google Workspace / Okta / Entra)
  configured on GitLab from day one with `auto_link_user` on email. The first
  SSO sign-in links the IdP identity to the ghost account automatically —
  the user keeps their username, history, keys and authorship. No admin work
  per user.
* Fallback: engine activates all accounts and triggers password-reset emails;
  users set a password and enable 2FA.
* Users not in the IdP (former staff, bots, outside collaborators) stay
  deactivated as historical authors — exactly what GitHub shows for deleted
  users, but better.
* GitHub bot accounts (dependabot, CI bots) map to GitLab project/group access
  tokens or service accounts, pre-created and documented.

---------------------------------------------------------------------------
## 17. CI/CD, secrets and the rest of the toolchain

The mirrored repository must stay byte-identical to GitHub, so GitLab-specific
files are *not* committed into mirrored repos while in MIRROR mode. Instead:

* **CI configuration lives out-of-tree.** Each mirrored project's "CI/CD
  configuration file" setting points at `pipelines/<org>/<repo>.gitlab-ci.yml@
  <org>/ci-config` — a GitLab-only project holding translated pipelines. Teams
  maintain the translation as part of normal work (a PR that changes
  `.github/workflows/*` fails a GitLab shadow pipeline until the translation
  is updated → a required check on the GitHub side keeps them in step).
  After promotion, teams may move the file in-tree at leisure.
* **Shadow pipelines:** nightly scheduled pipelines on the mirror of each
  default branch prove the translated CI passes on GitLab runners.
* **Runners** are provisioned and registered in both GitLab instances now, not
  after the incident (cloud runners in the cloud, on-prem runners for offline).
* **Secrets** cannot be read from GitHub, so they are never "mirrored". Move
  CI secrets into a secrets manager (Vault / cloud KMS) that both GitHub
  Actions and GitLab CI fetch via OIDC federation. Then no secret has to be
  re-entered during failover. Until that is done, maintain GitLab group-level
  CI variables as a mirrored checklist reviewed quarterly.
* **CODEOWNERS** works in GitLab from the same file paths — zero change.
* **Branch protection / rulesets** → protected branches + approval rules,
  applied at promote (§14).
* **Container & package registries:** GHCR images sync to the GitLab registry
  continuously (`skopeo sync`), npm/maven/pypi via package APIs. After failover
  only the registry hostname changes, hidden behind the vanity DNS (§18).
* **Releases & assets:** mirrored with identical tags and asset names; download
  URLs follow the vanity host pattern.
* **GitHub Pages** → GitLab Pages job in the out-of-tree pipeline.
* **Integrations** (Slack, Jira, issue trackers, deploy webhooks, status
  dashboards): configured on GitLab in disabled state in MIRROR mode; promote
  enables them. Keep a registry of every GitHub-side integration in L2.
* **Dependabot** → GitLab dependency scanning / Renovate on GitLab.
* **Actions marketplace steps** → GitLab CI/CD components; GitLab's
  experimental "run GitHub Actions in GitLab CI" can be used as a stop-gap for
  pipelines nobody has translated yet, but do not depend on it.

---------------------------------------------------------------------------
## 18. Making the switch invisible to developers

| Developer touchpoint | GitHub today | After promote | Change required |
|---|---|---|---|
| Clone / push URL | `github.com/org/repo` | `git.company.com/org/repo` | `git remote set-url` (scripted: `switch-remote.sh`), or a company-wide `url.<new>.insteadOf <old>` git config pushed by MDM |
| Issue / PR numbers | `#123` | `#123` | none (§15) |
| Links in docs/Slack/commits | github.com URLs | redirector rewrites to GitLab | none |
| SSH / GPG keys | user's keys | same keys pre-imported | none |
| Username, avatar, history | — | identical | none |
| Sign-in | GitHub SSO | company SSO on GitLab | first login auto-links |
| Code owners, branch rules | CODEOWNERS, rulesets | same file, equivalent rules | none |
| CI | Actions | pre-translated GitLab CI | none on day one; move in-tree later |
| CLI | `gh` | `glab` | learn ~10 commands; cheat-sheet in runbook |
| Vocabulary | Pull request | Merge request | none technical |
| Registry pulls | `ghcr.io/org/img` | `registry.company.com/org/img` | hostname, pre-mirrored |

The vanity host (`git.company.com`) is created on day one and serves the cloud
GitLab even while in MIRROR mode, so developers can already browse the mirror
and the DNS change at promote is only relevant if the offline instance is the
one promoted.

---------------------------------------------------------------------------
## 19. Proving readiness continuously

* **Monthly failover drill** into a scratch namespace or a snapshot of the
  offline instance: run `promote`, have two engineers clone, branch, open an MR,
  get it approved, pipeline green, merge, tag a release. Time it against the
  RTO. Run `demote`. Record results.
* **Daily parity report:** refs, issue/MR counts and iids, note counts, member
  roles, keys; any drift fails the report.
* **Shadow pipelines** (§17) are the CI readiness signal.
* **Synthetic account test:** one real test user claims a ghost account via SSO
  weekly to prove the account flip works.
* **Engine health:** sync lag, dead letters, circuit-breaker state exported as
  metrics with alerts.

---------------------------------------------------------------------------
## 20. Additions to the phased plan

| Phase | Added deliverable |
|---|---|
| 1 | Vanity DNS, runners registered, SSO configured with auto-link, deletion-propagation guard in the engine from the first commit |
| 2 | Verify seed import preserved iids; implement MR iid parity for incremental PRs; import SSH/GPG keys for all users |
| 3 | `promote` / `demote` commands; out-of-tree CI project and shadow pipelines; URL redirector |
| 4 | Secrets moved to a vault with OIDC from both CI systems; integrations pre-configured disabled |
| 5 | First full failover drill; runbook signed off; drill cadence scheduled |

===========================================================================
# PART C — AS BUILT (engine v0.1, 2026-10-31)

Code: `engine/` (Python 3.12, ~3.8k lines, deps: requests, PyYAML, PyJWT; psycopg for
PostgreSQL). Deployment: `deploy/`. Operations: `docs/RUNBOOK.md`. End-to-end test:
`e2e/run_e2e.sh`. Build-time findings: ROADBLOCKS.md §E.

## 21. What is implemented

| Area | Status |
|---|---|
| L1 git: bare mirrors, ref-exact push of branches + tags to every target, pruning, force-push mirroring, deleted/rewritten tips preserved in `refs/ghmirror-backup/`, wiki repos, LFS when `git-lfs` is installed | built; wiki/LFS paths not exercised by the e2e (GitHub wikis cannot be created by API; no LFS on the test host) |
| L2 archive: repo, issues, PRs (+comments, reviews, review comments, threads, reactions), labels, milestones, releases + asset files, attachments, branch protection, rulesets, collaborators, users, org/teams; history of every changed object; weekly GitHub migration archive | built; migration archive needs a classic PAT (OAuth tokens are refused, see E8) |
| L3: projects (visibility, topics, description, default branch, archived, renames), labels, milestones, issues (number, author, dates, state, closer, labels, milestone, assignees, lock), comments (edit, delete), reactions → award emoji, PRs → MRs (number parity, exact diffs, open/closed/merged with merger and dates, drafts, reviewers), reviews → notes + approvals, line comments → positioned diff threads (single and multi-line, replies, resolution), releases + assets via generic packages, GitHub-hosted attachments re-hosted | built and e2e-tested |
| Identity: ghost accounts (blocked group Owners), adoption by `github_id` marker, collisions, bots, renames, SSH/GPG keys, avatars, identity map for company emails | built and e2e-tested (except renames) |
| Org: members, teams → subgroups, team repo access → project shares, collaborators → project members (applied at promote) | built; unit/e2e coverage limited: the test account is not an organization |
| Engine: webhook receiver (HMAC, dedupe), durable queue, workers, scheduler, `/metrics`, `/status`, alerts webhook, audit log | built and e2e-tested |
| Deletion guard: owner 404 → halt; mass disappearance of repos/issues → halt; single repo deletion → archive; deleted issues → closed + labelled | built and e2e-tested |
| Promote/demote: real branch rules, CI on, accounts unblocked with company emails, bots stay blocked, real memberships, banner, push mirrors to the secondary (outbound allowlist, `mirror_url`); crash-safe record; full reversal | built and e2e-tested (incl. a promote killed midway) |
| Verify: refs, issue/MR numbers, titles, authors, dates, states, merger, labels, note counts, milestones, releases, and objects present only on GitLab; `--repair` | built and e2e-tested |
| URL redirector | built and e2e-tested |
| GitHub App authentication | built; the e2e used a token (no App on the test account) |

## 22. Not built (yet)

* GitLab → GitLab **metadata** sync after promotion. The secondary receives git data
  through GitLab push mirrors; issues/MRs created after failover reach it only through
  GitLab project export/import (manual) until a GitLab source adapter exists.
* Packages and container registries (`skopeo sync`), GitHub Discussions / Projects v2 /
  Actions run history into L2, Pages.
* CI translation and shadow pipelines (process, not engine), SSO configuration
  (GitLab setting), vanity DNS and webhook relay (infrastructure).
* A wrapper command for GitLab's importer seed; the engine adopts importer-seeded
  projects, but that combination was not exercised end to end.

## 23. End-to-end results (2026-10-31)

Setup: one 8 GB / 8-core laptop running two GitLab CE 19.5.3 containers (cloud, offline) in
a 4-CPU / 8 GB VM, a real personal GitHub account, `e2e/run_e2e.sh` (15 steps, 23 checks).
The host is below GitLab's sizing for even one instance, which made it a useful stress test:
5xx answers, 20-minute Rails boots and Sidekiq lag all occurred and are all handled.

| Run | Outcome | What it found (ROADBLOCKS §E) |
|---|---|---|
| 1 | 15 of 23 checks passed | E11 duplicate create after an ambiguous POST; E12 promote aborted before its undo record, push mirror URL/allowlist |
| 2 | stopped at step 8 | E13 side-channel agents orphaned on the GitLab host until it ran out of memory; e2e check script lacked 5xx retries |
| 3 | **all checks passed** (steps 11–15 re-run on run 3's state after a harness edit broke the original run at step 13) | E11b create landing after the lookup, E15 push mirror 5-min schedule, E16 branch-cache lag, E17 verify missed GitLab-only objects, E18 promote killed midway → demote restored everything |

Run 3 figures: seed of both targets 41 min (this host); 93 phase-1 and 115 phase-2 object
checks; parity 0 drift after seed, after webhook sync and after the drill; second full pass
made no writes; one repo deleted on GitHub kept and archived on both targets; vanished owner
halted with zero writes; promote → developer push/MR/merge on GitLab → merge reached the
offline secondary through the push mirror → demote → re-sync to 0 drift; redirector maps
`/pull/3` to `!3`. Skipped: the GitHub migration archive (needs a classic PAT, alert raised).
Unit tests: 32 (+6 PostgreSQL-only).

Not exercised end to end: GitHub organization teams (test account is personal), GitHub App
auth, wiki and LFS, renames of GitHub users, the migration archive.
