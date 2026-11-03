# Installation guide

This guide installs ghmirror the same way every time. Every input that changes the result is
pinned and listed in §1:
- the repository commit
- the base images, by digest
- the Python dependencies, by version
- the GitLab version
- your answers, which can be given as flags

The interactive path takes about 15 minutes plus GitLab boot time. The unattended path (§5) suits
configuration management.

Related: [SUPPORT-MATRIX.md](SUPPORT-MATRIX.md) (what is supported), [CAPACITY.md](CAPACITY.md)
(sizing), [SECURITY.md](SECURITY.md) (hardening), [RUNBOOK.md](RUNBOOK.md) (operations),
[`examples/`](../examples) (sample configuration).

## 1. What is pinned

| Input | Pinned to | Where |
|---|---|---|
| ghmirror itself | the git commit or tag you check out | `git checkout <tag>` |
| Engine base image | `python:3.12-slim@sha256:b18b261e…31ab30` | `deploy/compose/Dockerfile` |
| Python dependencies | exact versions (`requests 2.34.2`, `psycopg 3.3.7`, …) | `engine/requirements.lock` |
| PostgreSQL | `postgres:16-alpine@sha256:0cefaabd…5731ab` | `deploy/compose/docker-compose.yml` |
| GitLab (template) | `gitlab/gitlab-ce:19.5.3-ce.0@sha256:6863d156…78b27c` | `deploy/gitlab/docker-compose.yml` |
| Debian packages in the image (git, git-lfs, openssh-client) | whatever the pinned base image's Debian release ships on build day | not pinned; record `git --version` in your change log |

Two builds of the same commit therefore produce the same Python environment on the same base
image. Only the Debian security updates to git and OpenSSH can differ, which is intended.

## 2. Prerequisites

**Engine host** (one per installation):
- Linux VM, 2 vCPU, 4 GB RAM (add 1 GB per worker beyond 2 if repositories are larger than a few
  hundred MB: a large push peaks at about 0.8 GB in git, [CAPACITY.md](CAPACITY.md) §3.1), disk per
  [CAPACITY.md](CAPACITY.md) §1
- Docker Engine and Compose v2
- outbound HTTPS to `api.github.com`, `github.com` and both GitLabs
- outbound SSH to both GitLab hosts
- optional inbound HTTPS for webhooks; without it the engine polls every 15 minutes

**Each GitLab host** (one cloud, one on-premises):
- self-managed GitLab CE or EE 19.x (tested: 19.5.3)
- GitLab's recommended 8 GB RAM / 4 vCPU, plus about 1 GB for the side-channel agent
- an account that may `sudo`
- `sshd` reachable from the engine host

**People and accounts:**
- a GitHub organization owner, who clicks *Create* and *Install* for the GitHub App (or provides a token)
- a GitLab administrator on each GitLab host, who runs one command there

**Network flows:**

| From | To | Port | Purpose |
|---|---|---|---|
| engine | api.github.com, github.com | 443 | read API, `git fetch` |
| engine | each GitLab | 443 (or the GitLab URL's port) | API, `git push` |
| engine | each GitLab host | 22 (host `sshd`, not GitLab's git-SSH) | side channel |
| GitHub | your proxy → engine `127.0.0.1:8080` | 443 → 8080 | webhooks (optional; `POST /webhook` only) |
| GitLab A | GitLab B | 443 | push mirror, only after a failover |

## 3. GitLab hosts (skip if you already run GitLab)

On each GitLab host:

```sh
git clone https://github.com/<you>/ghmirror && cd ghmirror/deploy/gitlab
# edit external_url in docker-compose.yml, then:
docker compose up -d
docker compose logs -f gitlab        # wait for "gitlab Reconfigured!", 3–10 minutes
```

The template maps GitLab's git-over-SSH to port 2222, so the host's own `sshd` keeps port 22 for
the side channel.

## 4. Engine (interactive)

```sh
git clone https://github.com/<you>/ghmirror && cd ghmirror
git checkout <release tag>           # or a reviewed commit
cd deploy/compose && ./setup.sh
```

`setup.sh` goes through these steps:

1. **It builds** the image `ghmirror:local` from the pinned Dockerfile.
2. **It asks** for the GitHub organization, the authentication (GitHub App, recommended, or a
   token) and the webhook URL. A blank URL means polling.
3. **It creates the GitHub App.** It opens github.com with a pre-filled, read-only App
   definition. You click **Create GitHub App**, then **Install**. No IDs or keys are copied by
   hand.
4. **It prints one command per GitLab host**, which you run from the engine host or any machine
   with SSH access:
   ```sh
   ssh admin@gitlab.example.com sudo bash -s -- --pubkey "'ssh-ed25519 AAAA… ghmirror-engine'" < prepare-host.sh
   ```
   The command creates the `ghmirror` login and an exact sudo rule. It also creates the admin bot
   `github-mirror` and its token. It ends by printing one line:
   `ghmirror-target:eyJ1cmwiOi…`
5. **You paste that line** at the prompt for that target.
6. **It writes** `ghmirror.yaml`, `.env` (0600), `identities.yaml` and `secrets/`, then runs
   `ghmirror check`. Every line must read `ok`:
   ```
   github: acme (org), repos visible: 42
   target cloud: GitLab 19.5.3, group acme (id 2), bot github-mirror, mode mirror, side channel ok (Rails 19.5.3), token expires 2027-11-04 (365 days)
   target offline: GitLab 19.5.3, group acme (id 2), bot github-mirror, mode mirror, side channel ok (Rails 19.5.3), token expires 2027-11-04 (365 days)
   ```
7. **It starts** PostgreSQL and the engine, and queues the first full copy.

**Then:**
- Route `POST /webhook` from your public HTTPS name to `127.0.0.1:8080` (reverse proxy or
  tunnel). Expose nothing else ([SECURITY.md](SECURITY.md) §4.8).
- Fill in `identities.yaml` (GitHub login to company email). This is needed before a failover,
  not before mirroring.
- Watch the first copy:
  ```sh
  docker compose run --rm engine status
  ```
- When the copy is done, run the first parity check:
  ```sh
  docker compose run --rm engine verify --format markdown --output parity.md
  ```
  It must report **PASS** (see [`docs/samples/`](samples)).

## 5. Engine (unattended)

Every question has a flag. `--yes` fails instead of prompting. Prepare the GitLab hosts first with
`prepare-host.sh` (step 4 above) and keep their `ghmirror-target:` lines. The key in `--pubkey`
comes from a first `init` run, or from a key you generate and place at `secrets/id_ed25519`.

```sh
cd deploy/compose
GITHUB_TOKEN=ghp_… ./setup.sh --yes \
  --owner acme --owner-type org \
  --github-auth token \
  --webhook-url https://mirror.acme.example/webhook \
  --target-names cloud offline \
  --target "cloud=ghmirror-target:eyJ1cmwiOi…" \
  --target "offline=ghmirror-target:eyJ1cmwiOi…" \
  --alert-webhook https://hooks.slack.com/services/…
```

The GitHub App option needs two browser clicks, so a fully unattended install uses a token. You
can switch to an App later with `./setup.sh --github-auth app`. The output files are identical to
[`examples/`](../examples), apart from the generated secrets.

## 6. Verify the installation

| Check | Command | Expected |
|---|---|---|
| Connectivity, permissions, side channels, token expiry | `docker compose run --rm engine check` | exit 0, every target `ok` |
| Service health | `curl -s 127.0.0.1:8080/healthz` | `{"ok": true, "halted": false}` |
| Webhooks arrive | GitHub → App (or org) → Advanced → Recent deliveries | `202` responses |
| First copy finished | `docker compose run --rm engine status` | queue empty, no dead jobs |
| Parity | `docker compose run --rm engine verify --format markdown` | **PASS** |
| Failover works | a drill: RUNBOOK §6 (promote, then demote) | promote `complete`, demote restores mirror mode |

## 7. Upgrades

```sh
git fetch && git checkout <new tag>
cd deploy/compose && ./setup.sh --yes
```

This rebuilds the image from the new pins, keeps every answer and secret, re-runs `check` and
restarts the service. On start the engine creates any missing tables. There are no versioned
schema migrations yet (v0.1), so a release that changes the schema must say so in its notes.
Back up the state database before upgrading:
```sh
docker compose exec postgres pg_dump -U ghmirror ghmirror > ghmirror-db.sql
```
To roll back, restore that dump, check out the previous tag and run `./setup.sh --yes`.

## 8. Updating the pinned versions (maintainers)

```sh
# base images: current digest of a tag
docker pull python:3.12-slim && docker image inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
# Python dependencies: resolve inside the pinned base image
docker run --rm -v "$PWD/engine:/src:ro" python:3.12-slim@sha256:<digest> sh -c \
  'cp -r /src /tmp/e && rm -rf /tmp/e/.venv && pip install -q "/tmp/e[postgres]" && pip freeze --exclude ghmirror'
```

Update `Dockerfile`, `requirements.lock` and the compose files together. Then run the unit tests
and the e2e (`e2e/run_e2e.sh`) before tagging.

## 9. Uninstall

```sh
cd deploy/compose && docker compose --profile failover down -v   # -v deletes the mirrors, archive and state
```

On each GitLab host, remove the side-channel login with `userdel -r ghmirror` and
`rm /etc/sudoers.d/ghmirror`. Revoke the bot's token, or block the bot, in the GitLab admin area.
The mirrored groups stay in GitLab until you delete them. On GitHub, uninstall and delete the App.
