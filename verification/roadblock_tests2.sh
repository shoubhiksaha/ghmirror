#!/bin/bash
set -u
B=http://localhost:8929/api/v4; TOK=glpat-roadblocktest000000001
SCR=$(cd "$(dirname "$0")" && pwd)
R(){ printf '\n### %s\n' "$*"; }
api(){ local m=$1 p=$2; shift 2; curl -s -o /tmp/rb_body -w '%{http_code}' -X "$m" -H "PRIVATE-TOKEN: $TOK" "$B$p" "$@"; echo " $(head -c 300 /tmp/rb_body)"; }
j(){ jq -r "$1" /tmp/rb_body 2>/dev/null; }
PID=1; GID=$(curl -s -H "PRIVATE-TOKEN: $TOK" "$B/groups?search=acme" | jq '.[0].id')
cd "$SCR/testrepo"

R "R9 redo: branches off CURRENT main, then ff-merge push and squash push"
git fetch -q gl && git checkout -q -B main gl/main
git checkout -qb ff1 && echo ff > ff.txt && git add . && git commit -qm "ff1 work" && git checkout -q main
git checkout -qb sq1 && echo sq > sq.txt && git add . && git commit -qm "sq1 work" && git checkout -q main
git push -q gl ff1 sq1 2>/dev/null
api POST /projects/$PID/merge_requests -H "Sudo: alice" -d source_branch=ff1 -d target_branch=main -d title="ff MR" >/dev/null; FF=$(j .iid)
api POST /projects/$PID/merge_requests -H "Sudo: alice" -d source_branch=sq1 -d target_branch=main -d title="sq MR" >/dev/null; SQ=$(j .iid); echo "ff MR=$FF sq MR=$SQ"
echo "-- R9a: push main fast-forwarded to ff1 (GitHub 'merge commit'/'rebase' style: source head reachable)"
git merge -q --ff-only ff1 && git push -q gl main; sleep 10
api GET /projects/$PID/merge_requests/$FF >/dev/null; echo "ff MR state=$(j .state) merged_by=$(j .merge_user.username) merged_at=$(j .merged_at)"
echo "-- R9a2: true merge commit (non-ff) of a new branch"
git checkout -qb mc1 && echo mc > mc.txt && git add . && git commit -qm "mc1 work" && git checkout -q main && git push -q gl mc1
api POST /projects/$PID/merge_requests -H "Sudo: alice" -d source_branch=mc1 -d target_branch=main -d title="mc MR" >/dev/null; MC=$(j .iid)
git merge -q --no-ff --no-edit mc1 && git push -q gl main; sleep 10
api GET /projects/$PID/merge_requests/$MC >/dev/null; echo "mc MR state=$(j .state) merged_by=$(j .merge_user.username) merge_commit=$(j .merge_commit_sha)"
echo "-- R9b: squash (different sha) of sq1 onto main"
git merge -q --squash sq1 && git commit -qm "squash sq1" && git push -q gl main; sleep 10
api GET /projects/$PID/merge_requests/$SQ >/dev/null; echo "sq MR state=$(j .state) (squash on GitHub leaves GitLab MR open unless engine acts)"

R "R15 redo: diff discussion with position (show errors)"
api GET /projects/$PID/merge_requests/$SQ/versions; BASE=$(j '.[0].base_commit_sha'); HEAD=$(j '.[0].head_commit_sha'); START=$(j '.[0].start_commit_sha'); echo "base=$BASE head=$HEAD start=$START"
api POST /projects/$PID/merge_requests/$SQ/discussions -H "Sudo: dave" -d body="line comment" -d created_at=2022-02-02T02:02:02Z \
  -d "position[position_type]=text" -d "position[base_sha]=$BASE" -d "position[head_sha]=$HEAD" -d "position[start_sha]=$START" \
  -d "position[new_path]=sq.txt" -d "position[old_path]=sq.txt" -d "position[new_line]=1"
echo "note author=$(j .notes[0].author.username) created_at=$(j .notes[0].created_at) type=$(j .notes[0].type) new_line=$(j .notes[0].position.new_line)"
echo "-- reply in thread as alice (Sudo) with created_at (Developer -> expect now)"
DID=$(j .id); api POST /projects/$PID/merge_requests/$SQ/discussions/$DID/notes -H "Sudo: alice" -d body="reply" -d created_at=2022-02-03T00:00:00Z >/dev/null; echo "reply author=$(j .author.username) created_at=$(j .created_at)"

R "R21 blocked user who is project Owner: Sudo + created_at + iid (the mirror-mode account model)"
api POST /users -d username=erin -d name="erin Ghost" -d email=erin@mirror.example -d force_random_password=true -d skip_confirmation=true >/dev/null; ERIN=$(j .id)
api POST /projects/$PID/members -d user_id=$ERIN -d access_level=50 >/dev/null
api POST /users/$ERIN/block >/dev/null
api POST /projects/$PID/issues -H "Sudo: erin" -d title="GH issue 77" -d iid=77 -d created_at=2018-08-08T08:08:08Z >/dev/null; echo "blocked owner: status via body -> iid=$(j .iid) created_at=$(j .created_at) author=$(j .author.username) state=$(j .author.state)"
api POST /projects/$PID/issues/77/notes -H "Sudo: erin" -d body="note by blocked owner" -d created_at=2018-08-09T00:00:00Z >/dev/null; echo "note created_at=$(j .created_at) author=$(j .author.username)"

R "R22 GROUP-level Owner (not project member) + blocked: does created_at work via inherited ownership?"
api POST /users -d username=frank -d name="frank Ghost" -d email=frank@mirror.example -d force_random_password=true -d skip_confirmation=true >/dev/null; FRANK=$(j .id)
api POST /groups/$GID/members -d user_id=$FRANK -d access_level=50 >/dev/null; api POST /users/$FRANK/block >/dev/null
api POST /projects/$PID/issues/77/notes -H "Sudo: frank" -d body="note by blocked group owner" -d created_at=2018-08-10T00:00:00Z >/dev/null; echo "group-owner note created_at=$(j .created_at) author=$(j .author.username)"

R "R23 can a blocked/deactivated user still be listed as MR reviewer/assignee? (assignee_ids on MR via API)"
api PUT /projects/$PID/merge_requests/$SQ -d assignee_ids[]=$ERIN -d reviewer_ids[]=$FRANK >/dev/null; echo "assignees=$(j '[.assignees[].username]|join(",")') reviewers=$(j '[.reviewers[].username]|join(",")')"

R "R24 can the mirror bot push while ALL humans are blocked and branch rule is Maintainers+force? (yes above) ; can a blocked Owner push via git? (expect denied)"
curl -s -o /dev/null -w 'git-http as blocked erin -> %{http_code}\n' -u "erin:$TOK" "http://localhost:8929/acme/repo1.git/info/refs?service=git-receive-pack"

R "R25 user-level sign-in blocked but API via Sudo works: confirm erin cannot get a session (password login) -> blocked"
echo "(blocked users: 'cannot sign in' per docs; verified state=$(curl -s -H "PRIVATE-TOKEN: $TOK" $B/users/$ERIN | jq -r .state))"

R "R26 placeholder/import-user types visible? list users of type placeholder (none yet)"
api GET "/users?without_project_bots=true&per_page=50" >/dev/null; echo "users: $(j '[.[]|"\(.username):\(.state)"]|join(" ")')"
echo "== DONE2 =="
