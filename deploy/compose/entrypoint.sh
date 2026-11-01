#!/bin/sh
# Starts as root only to hand the mounted secrets (owned by whoever ran setup on the host) to the
# unprivileged ghmirror user with the permissions ssh insists on, then drops privileges.
set -e
if [ "$(id -u)" = 0 ]; then
  mkdir -p /secrets
  if [ -d /mnt/secrets ]; then
    cp -a /mnt/secrets/. /secrets/ 2>/dev/null || true
  fi
  chown -R ghmirror:ghmirror /secrets /data
  chmod 700 /secrets
  find /secrets -type f ! -name '*.pub' ! -name known_hosts -exec chmod 600 {} +
  exec setpriv --reuid=ghmirror --regid=ghmirror --init-groups env HOME=/home/ghmirror ghmirror -c /config/ghmirror.yaml "$@"
fi
exec ghmirror -c /config/ghmirror.yaml "$@"
