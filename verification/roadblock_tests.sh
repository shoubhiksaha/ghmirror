#!/bin/bash
# Empirical checks of DESIGN.md roadblocks against a local GitLab CE (container "gl", http://localhost:8929)
set -u
B=http://localhost:8929/api/v4
TOK=glpat-roadblocktest000000001
SCR=$(cd "$(dirname "$0")" && pwd)
R(){ printf '\n### %s\n' "$*"; }
api(){ # api METHOD path [curl args...]
  local m=$1 p=$2; shift 2
  curl -s -o /tmp/rb_body -w '%{http_code}' -X "$m" -H "PRIVATE-TOKEN: $TOK" "$B$p" "$@"; echo " $(head -c 400 /tmp/rb_body)"
}
j(){ jq -r "$1" /tmp/rb_body 2>/dev/null; }

echo "== waiting for GitLab health =="
for i in $(seq 1 120); do
  s=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8929/users/sign_in 2>/dev/null); [ "$s" = 200 ] && break; sleep 10
done; echo "readiness=$s after $((i*10))s"

echo "== create root PAT via rails runner =="
docker exec gl gitlab-rails runner "
u=User.find_by_username('root')
t=u.personal_access_tokens.create!(scopes: ['api','sudo','admin_mode','read_api','write_repository'], name: 'rb', expires_at: 300.days.from_now)
t.set_token('$TOK'); t.save!
puts 'token ok'
" 2>&1 | tail -1
R "version"; api GET /version

R "SETUP group/project/users"
api POST /groups -d name=acme -d path=acme -d visibility=private >/dev/null
GID=$(curl -s -H "PRIVATE-TOKEN: $TOK" "$B/groups?search=acme" | jq '.[0].id')
api POST /projects -d name=repo1 -d path=repo1 -d namespace_id=$GID -d visibility=private -d initialize_with_readme=false -d default_branch=main >/dev/null
PID=$(curl -s -H "PRIVATE-TOKEN: $TOK" "$B/projects/acme%2Frepo1" | jq .id); echo "project id $PID"
for u in alice bob carol dave; do
  api POST /users -d username=$u -d name="$u Ghost" -d email=$u@mirror.example -d force_random_password=true -d skip_confirmation=true >/dev/null
done
UID_(){ curl -s -H "PRIVATE-TOKEN: $TOK" "$B/users?username=$1" | jq '.[0].id'; }
ALICE=$(UID_ alice); BOB=$(UID_ bob); CAROL=$(UID_ carol); DAVE=$(UID_ dave); echo "alice=$ALICE bob=$BOB carol=$CAROL dave=$DAVE"
api POST /projects/$PID/members -d user_id=$ALICE -d access_level=30 >/dev/null   # Developer
api POST /projects/$PID/members -d user_id=$BOB -d access_level=30 >/dev/null
api POST /projects/$PID/members -d user_id=$CAROL -d access_level=30 >/dev/null
api POST /projects/$PID/members -d user_id=$DAVE -d access_level=50 >/dev/null    # Owner

R "R17 deactivate a freshly created user via API (expect 403 'not dormant'); block via API"
api POST /users/$BOB/deactivate
api POST /users/$CAROL/block
echo "-- fallback: deactivate bob via rails runner"
docker exec gl gitlab-rails runner "u=User.find_by_username('bob'); puts u.deactivate ? 'bob deactivated' : u.errors.full_messages" 2>&1 | tail -1
api GET /users/$BOB | head -c 0; echo "bob state: $(j .state)"; api GET /users/$CAROL | head -c 0; echo "carol state: $(j .state)"

R "push test repo (branches + refs/pull/7/head)"
cd "$SCR/testrepo" && git remote remove gl 2>/dev/null; git remote add gl http://root:$TOK@localhost:8929/acme/repo1.git
git push -q gl main feat1 feat2 feat3 2>&1 | tail -2; echo "branches pushed rc=$?"
R "R10 push refs/pull/* (GitHub hidden refs) to GitLab"
git push gl refs/pull/7/head:refs/pull/7/head 2>&1 | tail -3
git push gl refs/pull/7/head:refs/heads/gh-pr/7 2>&1 | tail -2
git ls-remote gl | grep -E 'pull|gh-pr'

R "R13 protected branch: default protection on main; set allow_force_push + maintainers; force-push as bot"
api GET /projects/$PID/protected_branches | head -c 300; echo
api DELETE /projects/$PID/protected_branches/main >/dev/null
api POST /projects/$PID/protected_branches -d name='*' -d push_access_level=40 -d merge_access_level=40 -d allow_force_push=true | head -c 200; echo
git checkout -q main && git commit -q --amend -m "c1 amended" && git push -f gl main 2>&1 | tail -1; echo "force push rc=$?"

R "issue for note tests"
api POST /projects/$PID/issues -d title="Issue one" >/dev/null; ISS=$(j .iid); echo "issue iid=$ISS"

R "R1 Sudo as ACTIVE Developer (alice): create note"
api POST /projects/$PID/issues/$ISS/notes -H "Sudo: alice" -d body="hello from alice" | head -c 0; echo "status above; author=$(j .author.username)"
R "R4 created_at honored when Sudo as Developer? (expect ignored)"
api POST /projects/$PID/issues/$ISS/notes -H "Sudo: alice" -d body="backdated by dev" -d created_at=2020-01-02T03:04:05Z | head -c 0; echo "created_at=$(j .created_at) author=$(j .author.username)"
R "R5 created_at honored when Sudo as project OWNER (dave)?"
api POST /projects/$PID/issues/$ISS/notes -H "Sudo: dave" -d body="backdated by owner" -d created_at=2020-01-02T03:04:05Z | head -c 0; echo "created_at=$(j .created_at) author=$(j .author.username)"
R "R5b created_at as admin (root) without Sudo"
api POST /projects/$PID/issues/$ISS/notes -d body="backdated by admin" -d created_at=2020-01-02T03:04:05Z | head -c 0; echo "created_at=$(j .created_at) author=$(j .author.username)"
R "R3 Sudo as DEACTIVATED user (bob)"
api POST /projects/$PID/issues/$ISS/notes -H "Sudo: bob" -d body="hello from bob"
api GET /users/$BOB | head -c 0; echo "bob state after sudo: $(j .state)"
R "R2 Sudo as BLOCKED user (carol)"
api POST /projects/$PID/issues/$ISS/notes -H "Sudo: carol" -d body="hello from carol"

R "R6 issue with explicit iid + created_at via Sudo as Owner (dave) and as Developer (alice)"
api POST /projects/$PID/issues -H "Sudo: dave" -d title="GH issue 42" -d iid=42 -d created_at=2021-05-06T07:08:09Z | head -c 0; echo "iid=$(j .iid) created_at=$(j .created_at) author=$(j .author.username)"
api POST /projects/$PID/issues -H "Sudo: alice" -d title="GH issue 43" -d iid=43 -d created_at=2021-05-06T07:08:09Z | head -c 0; echo "iid=$(j .iid) created_at=$(j .created_at) author=$(j .author.username)"

R "R7 MR iid counter advance: first MR, then burn to land on iid 7"
api POST /projects/$PID/merge_requests -H "Sudo: alice" -d source_branch=feat1 -d target_branch=main -d title="PR 1" | head -c 0; echo "MR iid=$(j .iid) author=$(j .author.username) created_at=$(j .created_at)"
for k in 2 3 4 5 6; do
  api POST /projects/$PID/merge_requests -d source_branch=feat3 -d target_branch=main -d title="burn $k" >/dev/null; BI=$(j .iid)
  api DELETE /projects/$PID/merge_requests/$BI >/dev/null; printf 'burn iid=%s deleted; ' "$BI"
done; echo
api POST /projects/$PID/merge_requests -H "Sudo: alice" -d source_branch=feat2 -d target_branch=main -d title="PR 7" | head -c 0; echo "landed MR iid=$(j .iid) (want 7)"
MR7=$(j .iid)

R "R8 MR created_at via API (expect ignored) and via rails runner"
api POST /projects/$PID/merge_requests -d source_branch=feat3 -d target_branch=main -d title="PR 8" -d created_at=2019-01-01T00:00:00Z | head -c 0; echo "api created_at=$(j .created_at) iid=$(j .iid)"; MR8=$(j .iid)
docker exec gl gitlab-rails runner "
p=Project.find_by_full_path('acme/repo1'); m=p.merge_requests.find_by(iid: $MR8)
m.update_columns(created_at: Time.parse('2019-01-01T00:00:00Z'), updated_at: Time.parse('2019-01-02T00:00:00Z'), iid: 8)
puts 'runner updated'" 2>&1 | tail -1
api GET /projects/$PID/merge_requests/8 | head -c 0; echo "after runner: iid=$(j .iid) created_at=$(j .created_at) updated_at=$(j .updated_at)"

R "R11 approvals on CE via Sudo (alice approves MR 7)"
api POST /projects/$PID/merge_requests/$MR7/approve -H "Sudo: alice" | head -c 120; echo
api GET /projects/$PID/merge_requests/$MR7/approvals | head -c 0; echo "approved_by=$(j '[.approved_by[].user.username]|join(",")')"

R "R15 diff discussion with position + created_at via Sudo Owner (dave) on MR 7"
api GET /projects/$PID/merge_requests/$MR7/versions | head -c 0; BASE=$(j '.[0].base_commit_sha'); HEAD=$(j '.[0].head_commit_sha'); START=$(j '.[0].start_commit_sha')
api POST /projects/$PID/merge_requests/$MR7/discussions -H "Sudo: dave" -d body="line comment" -d created_at=2022-02-02T02:02:02Z \
  -d position[position_type]=text -d position[base_sha]=$BASE -d position[head_sha]=$HEAD -d position[start_sha]=$START \
  -d position[new_path]=c.txt -d position[old_path]=c.txt -d position[new_line]=1 | head -c 0
echo "disc note author=$(j .notes[0].author.username) created_at=$(j .notes[0].created_at) new_line=$(j .notes[0].position.new_line) type=$(j .notes[0].type)"

R "R9a merged-state detection: fast-forward main to feat2 and push -> MR 7 state?"
git checkout -q main && git merge -q --ff-only feat2 2>/dev/null || git merge -q --no-edit feat2; git push -q gl main 2>&1 | tail -1; sleep 8
api GET /projects/$PID/merge_requests/$MR7 | head -c 0; echo "MR7 state=$(j .state) merged_by=$(j .merge_user.username) merged_at=$(j .merged_at)"
R "R9b squash-merge on source: main gets a different commit with feat3 content -> MR 8 state?"
git checkout -q main && git merge -q --squash feat3 && git commit -qm "squash feat3" && git push -q gl main 2>&1 | tail -1; sleep 8
api GET /projects/$PID/merge_requests/8 | head -c 0; echo "MR8 state=$(j .state) (expect opened)"
R "R9c mark MR 8 merged via rails runner with merged_at/merged_by"
docker exec gl gitlab-rails runner "
p=Project.find_by_full_path('acme/repo1'); m=p.merge_requests.find_by(iid: 8); u=User.find_by_username('alice')
m.update_columns(state_id: MergeRequest.available_states[:merged], merge_commit_sha: p.repository.commit('main').id)
(m.metrics || m.build_metrics).update!(merged_by_id: u.id, merged_at: Time.parse('2019-01-03T00:00:00Z'))
puts 'merged via runner'" 2>&1 | tail -1
api GET /projects/$PID/merge_requests/8 | head -c 0; echo "MR8 state=$(j .state) merged_by=$(j .merge_user.username) merged_at=$(j .merged_at)"

R "R12 admin adds SSH key to alice"
ssh-keygen -q -t ed25519 -N '' -f /tmp/rb_key <<<y >/dev/null 2>&1
api POST /users/$ALICE/keys -d title=gh-key --data-urlencode key@/tmp/rb_key.pub | head -c 150; echo

R "R16 change user email: add secondary (admin, skip_confirmation) then make primary with skip_reconfirmation"
api POST /users/$ALICE/emails -d email=alice@company.example -d skip_confirmation=true | head -c 120; echo
api PUT /users/$ALICE -d email=alice@company.example -d skip_reconfirmation=true | head -c 0; echo "primary now: $(j .email)"

R "R14 external CI config path (Free?)"
api PUT /projects/$PID -d ci_config_path='ci/repo1.gitlab-ci.yml@acme/ci-config' | head -c 0; echo "ci_config_path=$(j .ci_config_path)"

R "R19 award emoji as alice with Sudo (no created_at param exists)"
api POST /projects/$PID/issues/$ISS/award_emoji -H "Sudo: alice" -d name=thumbsup | head -c 0; echo "award user=$(j .user.username) created_at=$(j .created_at)"

R "R20 MR author change + approvals backdate via runner (what API cannot do)"
docker exec gl gitlab-rails runner "
p=Project.find_by_full_path('acme/repo1'); m=p.merge_requests.find_by(iid: 7); a=m.approvals.first
a&.update_columns(created_at: Time.parse('2020-06-06T06:06:06Z')); puts 'approval backdated: '+(a&.created_at.to_s||'none')" 2>&1 | tail -1
echo; echo "== DONE =="
