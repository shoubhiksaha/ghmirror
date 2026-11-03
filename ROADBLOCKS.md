# Roadblock register — verified 2026-10-31

Method: every roadblock in DESIGN.md was either tested against a real GitLab CE
19.5.3 instance (Docker, `gitlab/gitlab-ce:latest`) with scripts in this
session, or checked against current GitLab/GitHub documentation and source.
Verdicts: **CLEAR** (works as designed), **CLEAR WITH CHANGE** (works, design
amended), **WORKAROUND** (needs the self-managed side channel), **OPEN**.

## A. Tested on GitLab CE 19.5.3

| # | Roadblock | Result | Verdict |
|---|---|---|---|
| A1 | Can the admin bot act as another user (`Sudo` header)? | Yes. Notes created with `Sudo: alice` show author alice. | CLEAR |
| A2 | Can it act as a **blocked** user? | Yes. `Sudo: carol` (blocked) → 201, author carol, state blocked. | CLEAR |
| A3 | Can it act as a **deactivated** user? | Yes. `Sudo: bob` (deactivated) → 201; bob stays deactivated (no auto-reactivation via API). | CLEAR |
| A4 | Is `created_at` honoured when acting as a **Developer**? | **No.** Backdate silently ignored (note stamped now). | CLEAR WITH CHANGE → A5 |
| A5 | Is `created_at` honoured when acting as a project **Owner**? | Yes, exact. Also as admin without Sudo. | CLEAR |
| A6 | …when acting as a **blocked** project Owner? | Yes: issue created with `iid=77`, `created_at=2018-08-08`, author erin (blocked). Notes too. | CLEAR |
| A7 | …when ownership is **inherited from the group** (blocked group Owner, not a project member)? | Yes. | CLEAR |
| A8 | …when acting as a **deactivated** Owner? | Yes. | CLEAR |
| A9 | Issue `iid` settable? | Yes (Owner/admin). Ignored for Developer. | CLEAR |
| A10 | MR `iid` settable via API? | No parameter. **Counter advance works**: created+deleted 5 throwaway MRs (iids 2–6), next real MR landed on iid 7 exactly. Deleted iids are not reused. | CLEAR |
| A11 | MR `created_at` via API? | Ignored. `gitlab-rails runner` `update_columns(created_at:, updated_at:, iid:)` → API returns the backdated values. | WORKAROUND |
| A12 | MR "merged" state when GitHub merges | **Merge-commit / rebase / fast-forward merges: automatic.** Pushing `main` containing the MR's head marked MR 9 `merged` within ~30 s (merged_by = mirror bot, merged_at = now). **Squash merges: MR stays `opened`** (different SHA). Rails runner sets `state_id=merged`, `merge_commit_sha`, `metrics.merged_by_id`, `metrics.merged_at` → API shows merged by alice at 2019-01-03. | CLEAR (non-squash) / WORKAROUND (squash + exact merged_by/merged_at) |
| A13 | Approvals on **CE/Free** via Sudo | `POST …/approve` as alice → 201, `approved_by=[alice]`. Approval `created_at` only via runner. | CLEAR (+WORKAROUND for timestamp) |
| A14 | Diff (line) comments via API with position + backdate | 201, `type=DiffNote`, `new_line=1`, author dave, created_at 2022-02-02. Thread replies via Sudo work (backdate again Owner-only). | CLEAR |
| A15 | Push GitHub hidden refs `refs/pull/N/head` to GitLab | Accepted (`[new reference]`), listed by `ls-remote`, invisible in UI. `refs/heads/gh-pr/N` also fine. | CLEAR |
| A16 | Force-push by mirror bot over protected branches | Project default protection on `main` exists; replaced with `*` rule, push=Maintainers, `allow_force_push=true` → bot force-push accepted. Role-based rules are Free; user-specific rules are Premium. | CLEAR |
| A17 | Deactivate a freshly created ghost user via API | 201 for a brand-new user with no activity. **403** for a user that has had API activity via Sudo ("active in the past 90 days"). Block via API always works. → Use **blocked** for mirror-mode ghosts. | CLEAR WITH CHANGE |
| A18 | Admin adds SSH key to another user | 201. (GPG endpoint documented identically.) | CLEAR |
| A19 | Change ghost's email to the real one at promote | Add secondary with `skip_confirmation=true`, then `PUT /users/:id email=… skip_reconfirmation=true` → primary switched. | CLEAR |
| A20 | External CI config path (keep mirrored repo byte-identical) | `ci_config_path=ci/repo1.gitlab-ci.yml@acme/ci-config` accepted on Free. | CLEAR |
| A21 | Award emoji as user | Works via Sudo; no `created_at` param (timestamp = now; runner if exactness matters). | CLEAR (minor) |
| A22 | Blocked/deactivated users as MR assignee/reviewer | Blocked erin accepted as assignee. | CLEAR |
| A23 | `POST /import/github` on self-managed | Import source disabled by default → enable via `PUT /application/settings import_sources[]=github`. A GitHub **OAuth** token (`gho_`) was accepted, not only a classic PAT. | CLEAR |

### Design changes resulting from A4–A8, A17
Mirror-mode ghost accounts are created **active → immediately blocked**, and
are given **Owner on the top-level mirror group**. Blocked = cannot sign in,
cannot use git or the API themselves, yet the admin bot can still act as them
with exact authorship, `created_at` and `iid`. Group Owner is what unlocks
backdating; it is harmless while blocked. At promote: real roles are applied
from the mapping DB, then accounts are unblocked (or claimed via SSO).

## B. Verified against documentation / source

| # | Roadblock | Finding | Verdict |
|---|---|---|---|
| B1 | Seed import preserves PR/issue numbers, timestamps, merged state | Importer sets `iid`, `created_at`, `updated_at`, `state_id` directly (`pull_request_importer.rb`). | CLEAR |
| B2 | Importer handles approvals / required status checks | No (comment instead) / not imported → engine adds approvals (A13), protection rules from L2. | CLEAR WITH CHANGE |
| B3 | Built-in GitLab mirroring syncs PRs | No ("doesn't sync new or updated pull requests") → engine needed. | CLEAR (expected) |
| B4 | Placeholder reassignment needs the destination user's acceptance | By default yes, but self-managed has the instance setting `allow_bypass_placeholder_confirmation` (`PUT /application/settings`). **Tested (C6):** with it on, an admin CSV upload reassigned every imported contribution to the ghost account with no user action. Self-managed has no placeholder limit. | CLEAR |
| B5 | GitHub member emails for ghost accounts | GraphQL `User.organizationVerifiedDomainEmails(login:)` returns emails on the org's verified domains; commits API gives `author.email` ↔ `author.login`; public email otherwise. Fallback alias + email swap at promote (A19). | CLEAR |
| B6 | GitHub App can read everything needed | Permissions: Contents, Issues, Pull requests, Discussions, Members, Administration (branch protection), Webhooks, Actions (logs), Custom org roles. | CLEAR |
| B7 | Org Migrations API with a GitHub App token | Docs: "only available to authenticated organization owners", PAT with `repo` + `admin:org`. Not documented for Apps → use a **service-account classic PAT** for the weekly archive (same token type the GitLab importer requires). Archive < 20 GB, deleted after 7 days. | CLEAR WITH CHANGE |
| B8 | SSO auto-link on self-managed Free | OmniAuth is Free on self-managed; `auto_link_user` links by primary or confirmed secondary email. | CLEAR |
| B9 | Premium-only features the design must avoid on CE | Pull mirroring, push rules, user/group-specific protected-branch rules, required approval rules, code-owner approval enforcement, group-level protected branches. Design uses role-based rules + blocked accounts; approvals are advisory on Free. | CLEAR (documented) |
| B10 | Deactivated users | Docs: deactivated users *can* sign in (auto-reactivated) → not a login lock. Blocked users cannot sign in. | CLEAR WITH CHANGE (use blocked) |

## C. Seed import end-to-end (tested: real GitHub repo → local GitLab CE)

Repo: a public GitHub repo of the user's with 3 issues, 4 PRs (3 merged,
1 open), 10 review comments and 3 reviews on one PR, 9 labels, 16 tags, 4
branches, external issue authors and two bots. `POST /import/github` into
group `acme`, finished in 290 s. Import stats: fetched = imported for every
type (issue 3, pull_request 4, diff_note 11, note 8, review 7, merged_by 3,
label 9, issue_event 13, collaborator 1).

| # | Check | Result | Verdict |
|---|---|---|---|
| C1 | Numbers | Issues iid 1–3 and MRs iid 4–7 identical to GitHub, sharing the one GitHub sequence. | CLEAR |
| C2 | Timestamps | `created_at` of every issue, MR and note matches GitHub to the second; `merged_at` exact (e.g. MR 6 merged 2026-10-28T22:03:55Z). | CLEAR |
| C3 | State | Merged PRs → `merged` with `merged_by`; open PR → `opened`; closed issues → `closed`. | CLEAR |
| C4 | Review comments | 11 review comments → `DiffNote` with line numbers where the position resolves (lines 10, 32, 163, 489), otherwise `LegacyDiffNote` carrying the hunk; reviews → notes prefixed "**Review:** Commented"; review-request and title-change events → system notes. | CLEAR (as importer source predicts) |
| C5 | Users | One placeholder user per GitHub identity (`<login>_placeholder_<id>`, `@noreply` email) including bots; CSV listing of all placeholders exposed at `GET /groups/:id/placeholder_reassignments` with GitHub numeric ids. | CLEAR |
| C6 | Reassign to ghost accounts | Set `allow_bypass_placeholder_confirmation=true`, created ghost `shoubhiksaha`, uploaded CSV mapping → MR author, merged_by and note authors all became `shoubhiksaha`, no acceptance step. | CLEAR |
| C7 | Approvals | `approved_by=[]` (importer never applies approvals) → engine adds them (A13). | CLEAR WITH CHANGE |
| C8 | Token | Import source must be enabled on self-managed (`import_sources[]=github`). A GitHub OAuth token worked; docs promise only classic PATs → use a service-account classic PAT in production. | CLEAR |
| C9 | Protected branches | Importer re-created protection on `main`; mirror bot rule must be applied after each import. | CLEAR (ordering) |

## E. Found while building and running the engine end to end

These only showed up under real load against a private repository; each is handled in `engine/`.

| # | Finding | Handling | Verdict |
|---|---|---|---|
| E1 | Right after a ghost account is made group Owner, or a project is created, impersonated calls return `404 Project Not Found` for a while: GitLab refreshes `project_authorizations` in a background job. | Client retries `Sudo` calls on that exact 404 with backoff (up to ~5 min). | CLEAR |
| E2 | A blocked user cannot create a **release** (`403 ... tag is protected`): GitLab checks git access for the release author. | Release created by the bot; real author set through the side channel (`release_meta`). | CLEAR WITH CHANGE |
| E3 | Branches covered by a protected rule (the mirror's `*`) **cannot be deleted by `git push`**. | Branch deletions (mirror pruning, helper branches) go through the branches API. | CLEAR WITH CHANGE |
| E4 | After a `git push`, GitLab's branch-name cache updates asynchronously, so creating an MR on a just-pushed branch can fail with `source_branch does not exist`. | Helper commits are pushed to hidden refs `refs/ghmirror/pull/N/*` (which also keep PR commits alive), helper branches are created through the API (cache updated synchronously); MR creation also retries. | CLEAR |
| E5 | Creating the MR for an already-merged PR against the real target gives an empty diff (target already contains the commits). | MR is created against a temporary branch at the PR's base SHA, so GitLab stores the PR's exact diff; the side channel then sets state, merger, dates and the real target branch; temporary branches are deleted. Verified: every merged/closed MR shows the same files as the PR. | CLEAR WITH CHANGE |
| E6 | If MR creation for PR N fails while a later PR succeeds, GitLab's MR counter passes N and number parity is lost. | Later MR creations in the same pass are deferred after a failure; existing misnumbered MRs are renumbered by the side channel (`mr_iid`: moves iid and the `refs/merge-requests/*/head` ref). Verified: !8, !9, !10 moved to !3, !4, !6. | CLEAR |
| E7 | A GitHub review and its line comments share author and timestamp, so matching existing GitLab notes by author + time alone adopts the wrong note. | Adoption also requires the same body and skips notes already mapped. | CLEAR |
| E8 | The GitHub migrations API refuses OAuth-app tokens (e.g. the `gh` CLI's `gho_` token) with 403. | Needs a classic PAT (as DESIGN §5 states); the engine raises a configuration alert instead of retrying. | CLEAR (config) |
| E9 | `gitlab-rails runner` boots in ~3–4 min on a loaded host. | The side channel is a persistent agent: one Rails boot per engine process, booted in the background while API work runs; ops stream as JSON lines. | CLEAR |
| E10 | Real accounts have bots that review every PR (Copilot, CodeRabbit): bot logins like `copilot-pull-request-reviewer[bot]`. | Mirrored as blocked users `<name>-bot`; they stay blocked at promote. | CLEAR |
| E11 | Under load a create (`POST`) can time out or get a 502 from GitLab's proxy **after** GitLab has created the object. Resending it blindly made `409 Duplicated issue` in the first full two-target run, and would duplicate notes, MRs (breaking number parity) and accounts. | Every create passes a lookup: when the outcome is unknown (timeout, 500/502/504) the client first looks for the object and adopts it, and only resends when it is absent; 429/503 (refused before any work) are resent directly. Issues also adopt on `409`. Unit-tested. | CLEAR |
| E12 | GitLab refuses push mirrors to private-network hosts (`Requests to the local network are not allowed`), which is exactly where an offline secondary lives; the mirror URL must also be the address the *primary server* uses, not the engine's. In run 1 this aborted `promote` before its undo record was written, so `demote` could not reverse it. | `promote` writes its record before the first change and after each step; a failing item is listed in `failures` and alerted while the rest continues; per-target `mirror_url`; the secondary's host is added to the outbound allowlist (removed by `demote`) and the first sync waits out GitLab's 60 s settings cache. Verified: promote → push mirror `finished` → demote restores blocked accounts, `*` rule, no mirrors, empty allowlist. | CLEAR WITH CHANGE |
| E13 | Killing the local `docker exec` / `ssh` client does **not** stop the `gitlab-rails runner` agent on the GitLab host, and a Rails process still booting ignores SIGTERM. Agents of engine runs that ended (or timed out mid-boot) piled up at ~0.7 GB each until the 8 GB test host ran out of memory and Puma stopped answering (run 2). | Each agent carries a unique token and is stopped on the host with `pkill -9 -f <token>` on close, boot timeout and failed warm-up; the agent also exits after `idle` seconds (default 900) without input, so even a SIGKILLed engine cannot leak one. Verified live: boot timeout → 0 left, clean close → 0 left, engine SIGKILL → agent gone within 75 s. | CLEAR |
| E14 | Two GitLab CE instances plus agents on one 8 GB machine is below GitLab's sizing (4 GB minimum *each*, 8 GB recommended); under the e2e's load both answered 502 at times and Rails boots took up to 20 min. | Engine and e2e checks retry 5xx with backoff; production sizing in RUNBOOK §1 stays at GitLab's recommendation (each instance on its own ≥ 8 GB host). | CLEAR (sizing) |
| E15 | After failover, GitLab updates a push mirror that covers all branches **at most once per 5 minutes** (`RemoteMirror::UNPROTECTED_BACKOFF_DELAY`; 1 minute when only protected branches are mirrored). Run 3's drill checked the secondary after ~2 min and failed. | Not a defect: the secondary lags the new primary by ≤ 5 min. Documented in RUNBOOK §5; the drill waits up to 8 min. Verified: a branch pushed to the promoted primary appeared on the offline secondary. | CLEAR (documented) |
| E16 | On a busy primary, a developer's MR on a just-pushed branch can fail with `source_branch does not exist` for minutes: the branch cache is refreshed by a Sidekiq job (same cause as E4). | GitLab behaviour, self-healing; the engine already retries this for its own MRs, and the drill now retries like a developer would. | CLEAR (GitLab behaviour) |
| E17 | `verify` compared GitHub → GitLab only: an MR or issue that exists on the mirror but not on GitHub (made by hand, a drill leftover) was not reported. | Reverse check added: such objects are drift (`github: false, gitlab: true`), reported and never deleted; items kept on purpose after a GitHub deletion (label `deleted-on-github`) are excluded. Verified: a stray MR !11 was reported. | CLEAR |
| E18 | Promote killed midway (SIGTERM during the drill). | Verified live: the record said `in_progress`; a second `promote` refused; `demote` restored blocked account, `*` rule, CI off, no mirrors, GitHub source. | CLEAR |
| E19 | Promote while the secondary GitLab was down: no push mirror was configured and promote still reported success, so the team would assume a second copy that did not exist. | An unreachable secondary is now a recorded failure: state `partial`, the failure names the secondary and how to add the mirrors later, exit code 1. Verified with the offline GitLab stopped. | CLEAR |
| E20 | Setup test, side channel over **SSH** (the e2e used `docker exec`): ssh joins its arguments into one remote shell string, so `sh -c "cat > /tmp/agent.rb"` had its redirect run by the login shell. With a Docker-hosted GitLab the agent file would land on the host, not in the container. | Remote arguments are shell-quoted when the transport is ssh (`SideChannel._cmd`). Verified: `check` reports `side channel ok` over real SSH + sudo. | CLEAR |
| E21 | `pkill -f <token>` over ssh also matches the `sudo`/`sh` process carrying the same token on its command line. | Pattern `[g]hmirror-agent-…` matches the agent but not itself. | CLEAR |
| E22 | sudo matches the exact path it resolves through `secure_path`: `/usr/bin/gitlab-rails` (a symlink) did not match a rule naming `/opt/gitlab/bin/gitlab-rails` → "a password is required". | `prepare-host.sh` lists every existing path of `gitlab-rails`, `sh`, `pkill` (or `docker`). Verified live. | CLEAR |
| E23 | Compose stack never run before: `identities.yaml` not mounted; redirector without `.env`; new named volumes `/data/mirrors`, `/data/archive` root-owned (engine runs as uid 1000); secrets unreadable when owned by another host uid; GitLab template mapped git-SSH onto the host's port 22. | Mounts added; data dirs created in the image; entrypoint hands `/mnt/secrets` to the engine user and drops privileges; template uses 2222. Verified by `setup.sh` from scratch. | CLEAR |
| E24 | First start: the engine connected while PostgreSQL was still initialising (connection refused). | TCP `pg_isready` healthcheck + `depends_on: service_healthy` (a socket check passes during init). | CLEAR |
| E25 | GitLab 19 reports a username clash as "A user, alias, or group already exists", which the `<login>-gh` fallback did not recognise; and for a personal account init named the group after the person, taking their username. | Fallback matches the new text (verified live); personal accounts get group `<login>-github`, organizations keep the org name. | CLEAR |
| E26 | The `ghmirror-target:` line (with all host keys) was ~1500 characters; macOS terminals accept at most 1024 per pasted line, so the paste was cut and the prompt waited forever. | One host key (ed25519 first): 344 characters. A bad paste re-prompts instead of aborting. Verified in a pseudo-terminal. | CLEAR |
| E27 | A new install raised a "parity drift" alert before the first copy had run. | Uncopied repositories are `pending` for one day, drift after that. Verified live. | CLEAR |
| E28 | With a GitHub App (the recommended auth) the weekly migration archive is always refused, so every App install would get the same error alert weekly. | Optional `source.auth.migration_token`; without one the pass is skipped quietly; a refusal alerts once. Unit-tested. | CLEAR |
| E29 | Sample-report drill: a branch created by hand on a mirror was removed by the next ref-exact push (`verify --repair`). Commits that existed only on GitLab would have been lost, contradicting "never destructive". | Before deleting or overwriting a ref on a target, `push` saves any commit the mirror does not have into `refs/ghmirror-backup/<time>/target/` (fetched from the target); if that fails the ref is left in place and reported. Unit test plus live: commit `80bed43` made on GitLab only was kept. | CLEAR |
| E30 | Capacity measurement: every push to a repository changed `pushed_at`/`updated_at` of the repository copy embedded in each PR (`head.repo`, `base.repo`), so every full pass stored a new ~35 KB version of every PR in L2 (all 5 PRs re-versioned after two pushes). At 10,000 PRs: ~350 MB/day of noise. | A new version is kept only when the object changed, ignoring the counters and timestamps of embedded repositories (`archive.VOLATILE_REPO_FIELDS`); the repository's own archive entry still records them. Unit-tested. | CLEAR |

| E31 | Large-history test (`git/git`): its git notes (`refs/notes/amlog`) never reached GitLab, and the engine's mirror fetched notes only on its first clone and never updated them. Branches, tags and all 85,999 commits were identical. | Notes are now part of the mirrored refs (`gitmirror.MIRRORED`): fetched, backed up when rewritten, pushed, and compared by `verify`. Existing mirrors gain the notes refspec on their next fetch. Unit test plus live: the existing mirror upgraded itself and the notes ref became identical on GitLab. | CLEAR |

## D. Overall verdict

Every roadblock is clearable. Nothing requires GitLab Premium, GitLab.com,
or any undocumented behaviour. Two items need the self-managed **side
channel** (`gitlab-rails runner` on the GitLab host, or SQL): exact
`created_at` on merge requests, and marking **squash-merged** PRs as merged
with the right `merged_by`/`merged_at`. Everything else is public API.

Changes fed back into DESIGN.md: ghost accounts are *blocked* group Owners;
the engine gets a DB side-channel adapter; the seed uses CSV reassignment
with confirmation bypass; a service-account classic PAT is used for the
GitLab importer and the GitHub migration archive; squash merges handled
explicitly.
