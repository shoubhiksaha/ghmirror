#!/usr/bin/env bash
# Prepare a self-managed GitLab host as a ghmirror target. Run once per GitLab host, as root.
# `ghmirror init` prints the exact command, typically run from the engine host:
#
#   ssh admin@gitlab.example.com sudo bash -s -- --pubkey "'ssh-ed25519 AAAA... ghmirror'" < prepare-host.sh
#
# It is idempotent and does four things:
#   1. Unix user `ghmirror`, reachable only with the engine's SSH key (side channel transport).
#   2. /etc/sudoers.d/ghmirror: passwordless sudo for exactly what the side channel runs.
#   3. GitLab admin user `github-mirror` (the mirror bot) and a personal access token for it.
#   4. Prints one `ghmirror-target:` line (URL, token, SSH login, host keys) to paste into `ghmirror init`.
#
# The side channel is equivalent to root on this host: see "Security note" in the guide.
set -euo pipefail

PUBKEY="" CONTAINER="" SSH_HOST="" SSH_PORT=22 BOT=github-mirror DAYS=365 MODE=""
usage() {
  cat <<EOF
usage: prepare-host.sh --pubkey "<ssh public key>" [options]
  --pubkey KEY        the engine's public key (secrets/id_ed25519.pub, printed by ghmirror init)
  --container NAME    GitLab runs in this Docker container (auto-detected when there is exactly one)
  --ssh-host HOST     name or IP the engine uses to reach this host (default: $(hostname -f 2>/dev/null || hostname))
  --ssh-port PORT     port of this host's own sshd, not GitLab's git-over-SSH port (default 22)
  --bot NAME          GitLab username of the mirror bot (default github-mirror)
  --token-days N      token lifetime in days (default 365; GitLab caps it at 400 unless changed)
EOF
  exit 2
}
while [ $# -gt 0 ]; do
  case "$1" in
    --pubkey) PUBKEY=$2; shift 2 ;;
    --container) CONTAINER=$2; shift 2 ;;
    --ssh-host) SSH_HOST=$2; shift 2 ;;
    --ssh-port) SSH_PORT=$2; shift 2 ;;
    --bot) BOT=$2; shift 2 ;;
    --token-days) DAYS=$2; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown option $1" >&2; usage ;;
  esac
done
say() { echo "[ghmirror] $*" >&2; }
die() { echo "[ghmirror] ERROR: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (sudo bash prepare-host.sh ...)"
case "$PUBKEY" in ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-*) ;; *) die "--pubkey must be an SSH public key line" ;; esac
[[ "$BOT" =~ ^[a-zA-Z0-9_.-]+$ ]] || die "--bot: letters, digits, _ . - only"
[[ "$DAYS" =~ ^[0-9]+$ ]] || die "--token-days must be a number"
[[ "$SSH_PORT" =~ ^[0-9]+$ ]] || die "--ssh-port must be a number"
SSH_HOST=${SSH_HOST:-$(hostname -f 2>/dev/null || hostname)}

# --- where does GitLab run? -------------------------------------------------------------------
if [ -n "$CONTAINER" ]; then
  MODE=docker
elif command -v gitlab-rails >/dev/null 2>&1; then
  MODE=omnibus
elif command -v docker >/dev/null 2>&1; then
  mapfile -t found < <(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /gitlab\/gitlab-(ce|ee)/ {print $1}')
  [ "${#found[@]}" = 1 ] || die "found ${#found[@]} running GitLab containers; pass --container NAME"
  CONTAINER=${found[0]} MODE=docker
else
  die "no gitlab-rails and no Docker here: is this the GitLab host?"
fi
if [ "$MODE" = docker ]; then
  DOCKER=$(command -v docker)
  "$DOCKER" exec "$CONTAINER" test -x /opt/gitlab/bin/gitlab-rails || die "container $CONTAINER is not an Omnibus GitLab"
  rails() { "$DOCKER" exec -i "$CONTAINER" gitlab-rails "$@"; }
  REMOTE='["sudo", "docker", "exec", "-i", "'"$CONTAINER"'"]'
  SUDO_CMDS=$(for c in "$DOCKER" /usr/local/bin/docker /usr/bin/docker /bin/docker /snap/bin/docker; do
    if [ -x "$c" ]; then echo "$c exec -i $CONTAINER *"; fi; done | awk '!seen[$0]++' | paste -sd, - | sed 's/,/, /g')
else
  rails() { gitlab-rails "$@"; }
  REMOTE='["sudo"]'
  # sudo looks the name up in its secure_path and matches that exact path (a symlink is not its
  # target), so list every location a name can resolve to
  SUDO_CMDS=$(for n in gitlab-rails sh pkill; do
    for c in "$(command -v $n)" /usr/local/bin/$n /usr/bin/$n /bin/$n /usr/sbin/$n /sbin/$n /opt/gitlab/bin/$n; do
      if [ -n "$c" ] && [ -x "$c" ]; then echo "$c"; fi; done; done | awk '!seen[$0]++' | paste -sd, - | sed 's/,/, /g')
fi
say "GitLab: $MODE${CONTAINER:+ (container $CONTAINER)}"

# --- 1. side-channel user ---------------------------------------------------------------------
if ! id ghmirror >/dev/null 2>&1; then
  useradd --create-home --shell /bin/sh --comment "ghmirror side channel" ghmirror
  say "created Unix user ghmirror"
fi
# "*" = no password login, but not "locked" ("!"), which sshd without PAM treats as no login at all
usermod -p '*' ghmirror
HOME_DIR=$(getent passwd ghmirror | cut -d: -f6)
install -d -m 700 -o ghmirror -g ghmirror "$HOME_DIR/.ssh"
AK="$HOME_DIR/.ssh/authorized_keys"
touch "$AK"
KEYBODY=$(echo "$PUBKEY" | awk '{print $2}')
if ! grep -q "$KEYBODY" "$AK"; then
  echo "no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty $PUBKEY" >> "$AK"
  say "authorized the engine's SSH key"
fi
chown ghmirror:ghmirror "$AK"; chmod 600 "$AK"

# --- 2. sudo rule -----------------------------------------------------------------------------
command -v sudo >/dev/null 2>&1 || die "sudo is not installed"
TMP=$(mktemp)
echo "ghmirror ALL=(root) NOPASSWD: $SUDO_CMDS" > "$TMP"
visudo -cf "$TMP" >/dev/null || { rm -f "$TMP"; die "generated sudoers rule is invalid"; }
install -m 440 "$TMP" /etc/sudoers.d/ghmirror; rm -f "$TMP"
say "sudo rule: $SUDO_CMDS"

# --- 3. bot account and token (one Rails boot, can take a few minutes) ----------------------------
say "creating the bot account and token (Rails boot, please wait)..."
OUT=$(rails runner "
bot = '$BOT'
u = User.find_by_username(bot)
unless u
  admin = User.admins.active.order(:id).first or raise 'no active admin user to create the bot with'
  pw = SecureRandom.base64(24) + 'aA1!'
  params = { username: bot, name: 'GitHub Mirror', email: bot + '@ghmirror.invalid', password: pw,
             password_confirmation: pw, admin: true, skip_confirmation: true }
  params[:organization_id] = Organizations::Organization.first.id if defined?(Organizations::Organization) && Organizations::Organization.respond_to?(:first) && Organizations::Organization.first
  r = Users::CreateService.new(admin, params).execute
  u = r.respond_to?(:payload) ? r.payload[:user] : r
  raise 'creating the bot failed: ' + (r.respond_to?(:message) ? r.message.to_s : u.errors.full_messages.join(', ')) unless u && u.persisted?
end
u.update!(admin: true) unless u.admin?
u.activate if u.respond_to?(:blocked?) && u.blocked? && u.respond_to?(:activate)
t = u.personal_access_tokens.create!(name: 'ghmirror ' + Time.now.utc.strftime('%Y-%m-%d %H:%M'),
      scopes: %w[api sudo admin_mode read_api write_repository], expires_at: $DAYS.days.from_now.to_date)
puts 'GHM_TOKEN=' + t.token
puts 'GHM_URL=' + Gitlab.config.gitlab.url
puts 'GHM_VERSION=' + Gitlab::VERSION
" 2>&1) || { echo "$OUT" | tail -20 >&2; die "gitlab-rails runner failed"; }
TOKEN=$(echo "$OUT" | sed -n 's/^GHM_TOKEN=//p')
URL=$(echo "$OUT" | sed -n 's/^GHM_URL=//p')
VER=$(echo "$OUT" | sed -n 's/^GHM_VERSION=//p')
[ -n "$TOKEN" ] || { echo "$OUT" | tail -20 >&2; die "no token in Rails output"; }
say "bot $BOT ready on GitLab $VER at $URL (token expires in $DAYS days)"

# --- 4. the line for `ghmirror init` --------------------------------------------------------------
# one host key only, strongest first: the whole line must stay well under 1024 characters, the most a
# terminal accepts in one pasted line (macOS), and an RSA key alone is ~550
KEYS=""
for t in ed25519 ecdsa rsa; do
  if [ -f /etc/ssh/ssh_host_${t}_key.pub ]; then
    KEYS=$(awk '{printf "\"%s %s\"", $1, $2}' /etc/ssh/ssh_host_${t}_key.pub); break
  fi
done
[ -n "$KEYS" ] || say "warning: no /etc/ssh/ssh_host_*_key.pub; init will fall back to ssh-keyscan"
JSON="{\"url\": \"$URL\", \"token\": \"$TOKEN\", \"ssh\": \"ghmirror@$SSH_HOST\", \"ssh_port\": $SSH_PORT, \"remote\": $REMOTE, \"host_keys\": [$KEYS]}"
echo
echo "=== GitLab host ready. Paste this line into 'ghmirror init' (it contains the bot's token): ==="
echo "ghmirror-target:$(printf '%s' "$JSON" | base64 | tr -d '\n')"
