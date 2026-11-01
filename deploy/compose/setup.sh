#!/usr/bin/env bash
# One-command setup of the ghmirror engine on this host (needs only Docker):
#   1. builds the engine image
#   2. `ghmirror init`: asks a few questions, creates the GitHub App, writes ghmirror.yaml/.env/secrets
#   3. `ghmirror check`: GitHub access, both GitLabs, side channels
#   4. starts PostgreSQL + the engine service and queues the first full copy
# Safe to re-run: existing answers are kept. Extra arguments go to `ghmirror init` (see --help there),
# e.g. ./setup.sh --owner acme --webhook-url https://mirror.acme.com
set -euo pipefail
cd "$(dirname "$0")"
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die() { echo "setup: $*" >&2; exit 1; }

command -v docker >/dev/null || die "Docker is required: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (docker compose ...)"
PORT=${GHM_CALLBACK_PORT:-8765}

step "1/4 Building the engine image"
docker build -q -f Dockerfile -t ghmirror:local ../.. >/dev/null && echo "image ghmirror:local built"

step "2/4 Configuration"
TTY=-i; [ -t 0 ] && [ -t 1 ] && TTY=-it
docker run --rm $TTY --user "$(id -u):$(id -g)" -e HOME=/tmp -e GITHUB_TOKEN="${GITHUB_TOKEN:-}" -e GHM_HOST_DIR="$PWD" \
  -v "$PWD:/setup" -p "127.0.0.1:$PORT:$PORT" --entrypoint ghmirror ghmirror:local \
  init --dir /setup --callback-bind 0.0.0.0 --callback-port "$PORT" "$@"

step "3/4 Checking GitHub, GitLab and the side channels (first Rails boot on a GitLab host takes minutes)"
docker compose run --rm engine check || die "check failed: fix the line marked FAILED/UNAVAILABLE, then re-run ./setup.sh"

step "4/4 Starting the service and queueing the first full copy"
docker compose up -d postgres engine
docker compose run --rm engine enqueue-all

cat <<EOF

ghmirror is running. The first copy proceeds in the background (about one API call per object).
  progress     docker compose run --rm engine status
  logs         docker compose logs -f engine
  parity       docker compose run --rm engine verify      (expect "drift_count": 0 once the copy is done)
Before a failover is ever needed: fill in identities.yaml, then run a drill (guide, section 7).
EOF
