#!/usr/bin/env bash
# Prepare a fresh GitLab CE container as a mirror target: wait for boot, create an admin token.
# usage: bootstrap_gitlab.sh CONTAINER PORT TOKEN
set -euo pipefail
C=$1 PORT=$2 TOK=$3
until [ "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/users/sign_in")" = 200 ]; do sleep 10; done
docker exec "$C" gitlab-rails runner "
u = User.find_by_username('root')
unless PersonalAccessToken.find_by_token('$TOK')
  t = u.personal_access_tokens.create!(scopes: %w[api sudo admin_mode read_api write_repository], name: 'ghmirror-bot', expires_at: 360.days.from_now)
  t.set_token('$TOK'); t.save!
end
s = ApplicationSetting.current
s.update!(signup_enabled: false)
puts 'bootstrap ok'
" 2>&1 | tail -1
curl -s -H "PRIVATE-TOKEN: $TOK" "http://localhost:$PORT/api/v4/version"; echo
