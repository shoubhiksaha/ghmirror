#!/usr/bin/env bash
# Full end-to-end test of ghmirror against a real GitHub account and two GitLab CE instances.
# Prereqs: GitLab containers glc (:8931) and glo (:8932) bootstrapped (bootstrap_gitlab.sh), `gh` logged in.
# Creates (and at the start deletes) private repos <owner>/ghmirror-e2e-source and ghmirror-e2e-doomed.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"
export GITHUB_TOKEN=${GITHUB_TOKEN:-$(gh auth token)}
export E2E_STATE=$HERE/state/run
export E2E_GROUP=${E2E_GROUP:-ghmirror-e2e-$(date +%m%d%H%M)}   # fresh top-level group per run (GitLab keeps deleted top-level groups for days)
GE=$E2E_GROUP
OWNER=$(gh api user --jq .login)
PY=$HERE/../engine/.venv/bin/python
GM="$PY -m ghmirror -c $HERE/state/config.yaml"
URL=http://127.0.0.1:8099
SECRET=e2e-webhook-secret
CLOUD="-H PRIVATE-TOKEN:glpat-e2ecloud000000000000001 http://localhost:8931/api/v4"
OFF="-H PRIVATE-TOKEN:glpat-e2eoffline00000000000001 http://localhost:8932/api/v4"
RESULTS=()
step() { echo; echo "=================== $* ==================="; }
record() { RESULTS+=("$1|$2"); echo ">>> $1: $2"; }
glc() { local p=$1; shift; curl -s -H "PRIVATE-TOKEN: glpat-e2ecloud000000000000001" "$@" "http://localhost:8931/api/v4$p"; }
glo() { local p=$1; shift; curl -s -H "PRIVATE-TOKEN: glpat-e2eoffline00000000000001" "$@" "http://localhost:8932/api/v4$p"; }
hook() { $PY webhook.py $URL/webhook $SECRET "$@"; }
settle() {  # bots (Copilot, CodeRabbit...) review new PRs asynchronously: wait until GitHub stops changing
  local prev="" cur
  for _ in $(seq 1 16); do
    cur=$(gh api "repos/$OWNER/ghmirror-e2e-source/issues?state=all&per_page=100" --jq '[.[] | .comments] | add')-$(for n in $(gh api "repos/$OWNER/ghmirror-e2e-source/pulls?state=all&per_page=100" --jq '.[].number'); do gh api repos/$OWNER/ghmirror-e2e-source/pulls/$n/reviews --jq length; done | paste -sd+ - | bc)
    [ "$cur" = "$prev" ] && { echo "GitHub settled ($cur)"; return 0; }
    prev=$cur; sleep 45; done; echo "GitHub still changing ($cur)"; }
drain() {  # wait until the service queue is empty
  for _ in $(seq 1 360); do
    s=$(curl -s $URL/status | $PY -c 'import json,sys;j=json.load(sys.stdin)["jobs"];print(j.get("pending",0)+j.get("running",0))' 2>/dev/null)
    [ "$s" = 0 ] && return 0; sleep 5; done; return 1; }

step "0. reset state (GitHub scratch repos, engine state; GitLab group: $GE)"
for r in ghmirror-e2e-source ghmirror-e2e-doomed; do gh api -X DELETE repos/$OWNER/$r >/dev/null 2>&1 && echo "deleted GitHub $r"; done
rm -rf "$E2E_STATE"; mkdir -p "$E2E_STATE"
# the GitLab servers reach each other by name (push mirrors after promote), as two hosts on a LAN would
docker network inspect ghm-net >/dev/null 2>&1 || docker network create ghm-net >/dev/null
for c in glc glo; do docker network connect ghm-net $c 2>/dev/null; done

step "1. build GitHub fixture (phase 1)"
$PY github_fixture.py phase1 $OWNER ghmirror-e2e-source > state/fixture.json && cat state/fixture.json
record "fixture phase 1" "$([ -s state/fixture.json ] && echo ok || echo FAIL)"
settle

step "2. ghmirror check"
$GM check; record "check" "$([ $? = 0 ] && echo ok || echo FAIL)"

step "3. seed: ghmirror sync --full (both targets)"
t0=$(date +%s); $GM sync --full; rc=$?; t1=$(date +%s)
record "seed sync (rc=$rc, $((t1-t0))s)" "$([ $rc = 0 ] && echo ok || echo FAIL)"

step "4. phase-1 assertions + parity report"
$PY assert_mirror.py phase1 state/config.yaml | tee state/assert1.txt | grep -E "FAIL|PASSED|FAILED"
record "phase-1 object assertions ($(grep -c ^PASS state/assert1.txt) checks)" "$(grep -q 'ALL PASSED' state/assert1.txt && echo ok || echo FAIL)"
$GM verify | tee state/verify1.txt | head -5; record "parity report after seed" "$(head -1 state/verify1.txt | grep -q '"drift_count": 0' && echo ok || echo FAIL)"

step "5. idempotency: a second full pass must not create anything"
snap() { for g in glc glo; do $g "/projects/$GE%2Fghmirror-e2e-source/merge_requests?scope=all&per_page=100" | $PY -c 'import json,sys;print(sorted((m["iid"],m["user_notes_count"]) for m in json.load(sys.stdin)))'; $g "/projects/$GE%2Fghmirror-e2e-source/issues?scope=all&per_page=100" | $PY -c 'import json,sys;print(sorted((m["iid"],m["user_notes_count"],m["upvotes"]) for m in json.load(sys.stdin)))'; done; }
before=$(snap); a0=$(sqlite3 $E2E_STATE/ghmirror.db "select count(*) from audit where action not like 'sidechannel%'")
$GM sync --full >/dev/null 2>&1; after=$(snap); a1=$(sqlite3 $E2E_STATE/ghmirror.db "select count(*) from audit where action not like 'sidechannel%'")
record "idempotent re-run (objects unchanged, audit $a0 -> $a1)" "$([ "$before" = "$after" ] && [ "$a0" = "$a1" ] && echo ok || echo FAIL)"

step "6. start service (webhooks + workers + scheduler)"
$GM run > $E2E_STATE/service.log 2>&1 &
SVC=$!; for _ in $(seq 1 30); do curl -s $URL/healthz >/dev/null && break; sleep 1; done
record "service up" "$(curl -s $URL/healthz | grep -q '"ok": true' && echo ok || echo FAIL)"
code=$(hook ping $OWNER/ghmirror-e2e-source '{"_bad_signature": true}' | cut -d' ' -f1)
record "bad webhook signature rejected ($code)" "$([ "$code" = 401 ] && echo ok || echo FAIL)"

step "7. GitHub changes after first sync (phase 2) + webhook deliveries"
$PY github_fixture.py phase2 $OWNER ghmirror-e2e-source > state/fixture2.json && cat state/fixture2.json
settle
for ev in issues issue_comment pull_request push delete create release label; do hook $ev $OWNER/ghmirror-e2e-source '{"action":"edited"}' >/dev/null; done
d1=$(hook issues $OWNER/ghmirror-e2e-source '{"_delivery":"fixed-delivery-1"}'); d2=$(hook issues $OWNER/ghmirror-e2e-source '{"_delivery":"fixed-delivery-1"}')
record "duplicate delivery ignored" "$(echo "$d2" | grep -q duplicate && echo ok || echo FAIL)"
t0=$(date +%s); drain; t1=$(date +%s)
record "webhook-driven incremental sync drained in $((t1-t0))s" "$(curl -s $URL/status | grep -q '"dead"' && echo FAIL || echo ok)"

step "8. phase-2 assertions + parity report"
$PY assert_mirror.py phase2 state/config.yaml | tee state/assert2.txt | grep -E "FAIL|PASSED|FAILED"
record "phase-2 object assertions ($(grep -c ^PASS state/assert2.txt) checks)" "$(grep -q 'ALL PASSED' state/assert2.txt && echo ok || echo FAIL)"
$GM verify | tee state/verify2.txt | head -5; record "parity report after incremental sync" "$(head -1 state/verify2.txt | grep -q '"drift_count": 0' && echo ok || echo FAIL)"

step "9. deletion guard: a repository deleted on GitHub is archived, never deleted"
$PY github_fixture.py doomed-create $OWNER ghmirror-e2e-doomed; hook repository $OWNER/ghmirror-e2e-doomed '{"action":"created"}'; drain
before=$(glc "/projects/$GE%2Fghmirror-e2e-doomed" | $PY -c 'import json,sys;print(json.load(sys.stdin).get("archived"))')
$PY github_fixture.py doomed-delete $OWNER ghmirror-e2e-doomed; hook repository $OWNER/ghmirror-e2e-doomed '{"action":"deleted"}'; drain
ac=$(glc "/projects/$GE%2Fghmirror-e2e-doomed" | $PY -c 'import json,sys;j=json.load(sys.stdin);print(j.get("archived"), j.get("open_issues_count"))')
ao=$(glo "/projects/$GE%2Fghmirror-e2e-doomed" | $PY -c 'import json,sys;j=json.load(sys.stdin);print(j.get("archived"))')
record "doomed repo mirrored (archived=$before) then GitHub-deleted -> GitLab copies kept+archived (cloud: $ac, offline: $ao)" \
  "$([ "$before" = False ] && [[ "$ac" == True* ]] && [ "$ao" = True ] && echo ok || echo FAIL)"
record "service still not halted after one deletion" "$(curl -s $URL/healthz | grep -q '"halted": null' && echo ok || echo FAIL)"
kill $SVC; wait $SVC 2>/dev/null

step "10. circuit breaker: source owner vanishes -> halt, zero writes"
sed "s/owner: $OWNER/owner: ghmirror-no-such-owner-$RANDOM/" state/config.yaml > state/config-gone.yaml
a0=$(sqlite3 $E2E_STATE/ghmirror.db "select count(*) from audit")
$PY -m ghmirror -c state/config-gone.yaml sync; rc=$?
a1=$(sqlite3 $E2E_STATE/ghmirror.db "select count(*) from audit")
$GM sync --no-meta >/dev/null 2>&1; rc2=$?
record "halt on vanished owner (rc=$rc, writes $a0->$a1, further syncs refused rc=$rc2)" "$([ $rc = 3 ] && [ "$a0" = "$a1" ] && [ $rc2 = 3 ] && echo ok || echo FAIL)"
$GM guard reset; record "guard reset" ok

step "11. promote cloud -> PRIMARY (failover drill)"
$GM promote --primary cloud --yes | tee state/promote.json
UID_=$(glc "/users?username=$OWNER" | $PY -c 'import json,sys;print(json.load(sys.stdin)[0]["id"])')
st=$(glc /users/$UID_ | $PY -c 'import json,sys;j=json.load(sys.stdin);print(j["state"], j["email"])')
bot=$(glc "/users?search=bot" | $PY -c 'import json,sys;print(",".join(u["state"] for u in json.load(sys.stdin) if u["username"].endswith("-bot")))')
P=$(glc "/projects/$GE%2Fghmirror-e2e-source" | $PY -c 'import json,sys;j=json.load(sys.stdin);print(j["id"], j["builds_access_level"])')
PID=${P%% *}
mirrors=$(glc /projects/$PID/remote_mirrors | $PY -c 'import json,sys;print(len(json.load(sys.stdin)))')
record "promote: human account active with company email ($st), bots stay blocked ($bot), CI on (${P#* }), push mirror to offline ($mirrors)" \
  "$([[ "$st" == "active shoubhik@company.test" ]] && [[ "$bot" != *active* ]] && [ "${P#* }" = enabled ] && [ "$mirrors" = 1 ] && echo ok || echo FAIL)"

step "12. developer keeps working on the promoted GitLab (clone, push, MR, merge)"
TOK=$(glc /users/$UID_/personal_access_tokens -X POST -d name=drill -d "scopes[]=api" -d "scopes[]=write_repository" -d expires_at=2027-01-01 | $PY -c 'import json,sys;print(json.load(sys.stdin)["token"])')
W=$(mktemp -d); git -c http.extraHeader="Authorization: Basic $(printf "%s:%s" "$OWNER" "$TOK" | base64)" clone -q http://localhost:8931/$GE/ghmirror-e2e-source.git $W/c
( cd $W/c && git checkout -q -b drill-change && echo drill > drill.txt && git add . && git -c user.name=dev -c user.email=dev@x commit -qm "drill change" \
  && git -c http.extraHeader="Authorization: Basic $(printf "%s:%s" "$OWNER" "$TOK" | base64)" push -q origin drill-change )
for _ in $(seq 1 30); do  # GitLab refreshes its branch cache after a push in Sidekiq; until then: "source_branch does not exist"
  MR=$(curl -s -H "PRIVATE-TOKEN: $TOK" -X POST http://localhost:8931/api/v4/projects/$PID/merge_requests -d source_branch=drill-change -d target_branch=main -d title="Drill MR" | $PY -c 'import json,sys;j=json.load(sys.stdin);print(j["iid"], j["author"]["username"])' 2>/dev/null)
  [ -n "$MR" ] && break; sleep 10; done
sleep 8
for _ in $(seq 1 30); do  # wait for GitLab's async mergeability check
  MG=$(curl -s -H "PRIVATE-TOKEN: $TOK" -X PUT http://localhost:8931/api/v4/projects/$PID/merge_requests/${MR%% *}/merge | $PY -c 'import json,sys;print(json.load(sys.stdin).get("state"))' 2>/dev/null)
  [ "$MG" = merged ] && break; sleep 5; done
record "developer ($OWNER) pushed, opened MR !${MR%% *} and merged it ($MG)" "$([ "$MG" = merged ] && echo ok || echo FAIL)"
MID=$(glc /projects/$PID/remote_mirrors | $PY -c 'import json,sys;print(json.load(sys.stdin)[0]["id"])')
# GitLab runs an all-branches push mirror at most once per 5 minutes (RemoteMirror::UNPROTECTED_BACKOFF_DELAY),
# and promote has just synced it: allow up to 8 minutes
for _ in $(seq 1 48); do glc /projects/$PID/remote_mirrors/$MID/sync -X POST >/dev/null; sleep 10
  OB=$(glo "/projects/$GE%2Fghmirror-e2e-source/repository/branches/main" | $PY -c 'import json,sys;print(json.load(sys.stdin)["commit"]["message"].splitlines()[0])' 2>/dev/null)
  [[ "$OB" == Merge* ]] && break; done
record "secondary (offline) receives the primary's new commits via push mirror ($OB)" "$([[ "$OB" == Merge* ]] && echo ok || echo FAIL)"

step "13. demote (end of drill) and resume mirroring from GitHub"
$GM demote
st=$(glc /users/$UID_ | $PY -c 'import json,sys;print(json.load(sys.stdin)["state"])')
prot=$(glc /projects/$PID/protected_branches | $PY -c 'import json,sys;print([p["name"] for p in json.load(sys.stdin)])')
record "demote: account blocked again ($st), bot-only protection back ($prot)" "$([ "$st" = blocked ] && [ "$prot" = "['*']" ] && echo ok || echo FAIL)"
glc /projects/$PID/merge_requests/${MR%% *} -X DELETE >/dev/null   # drill artefact (drills belong in a snapshot)
$GM sync --full >/dev/null 2>&1; rc=$?
$GM verify | tee state/verify3.txt | head -3
record "re-sync after drill restores exact parity on both targets (rc=$rc)" "$(head -1 state/verify3.txt | grep -q '"drift_count": 0' && echo ok || echo FAIL)"

step "14. GitHub migration archive (L2 nuclear copy)"
$GM archive-migration | tee state/migration.json
f=$($PY -c 'import json;print(json.load(open("state/migration.json")).get("path",""))')
mst=$($PY -c 'import json;print(json.load(open("state/migration.json")).get("state",""))')
n=$([ -f "$f" ] && tar tzf "$f" | grep -c '^repositories_\|pull_requests_\|issues_' || echo 0)
if [ "$mst" = unauthorized ]; then record "migration archive (GitHub requires a classic PAT; OAuth token refused, alert raised)" SKIP
else record "migration archive downloaded ($(du -h "$f" 2>/dev/null | cut -f1), $n metadata files)" "$([ -f "$f" ] && [ "$n" -gt 0 ] && echo ok || echo FAIL)"; fi

step "15. URL redirector"
$GM redirector --listen 127.0.0.1:8098 > /dev/null 2>&1 & RD=$!; sleep 2
loc=$(curl -s -o /dev/null -w '%{redirect_url}' http://127.0.0.1:8098/$OWNER/ghmirror-e2e-source/pull/3); kill $RD
record "github.com/$OWNER/ghmirror-e2e-source/pull/3 -> $loc" "$([ "$loc" = "http://localhost:8931/$GE/ghmirror-e2e-source/-/merge_requests/3" ] && echo ok || echo FAIL)"

step "SUMMARY"
fail=0; for r in "${RESULTS[@]}"; do printf '%-6s %s\n' "${r##*|}" "${r%|*}"; case "${r##*|}" in ok|SKIP) ;; *) fail=$((fail+1));; esac; done
echo; [ $fail = 0 ] && echo "E2E: ALL ${#RESULTS[@]} STEPS PASSED" || echo "E2E: $fail of ${#RESULTS[@]} STEPS FAILED"
exit $fail
