#!/bin/bash
set -u
B=http://localhost:8929/api/v4; TOK=glpat-roadblocktest000000001
R(){ printf '\n### %s\n' "$*"; }
api(){ local m=$1 p=$2; shift 2; curl -s -o /tmp/rb_body -w '%{http_code}' -X "$m" -H "PRIVATE-TOKEN: $TOK" "$B$p" "$@"; echo " $(head -c 400 /tmp/rb_body)"; }
j(){ jq -r "$1" /tmp/rb_body 2>/dev/null; }
PID=1
R "R9a recheck after time: ff MR 9 and list of MRs with state"
api GET "/projects/$PID/merge_requests?per_page=50" >/dev/null; j '.[]|"iid=\(.iid) src=\(.source_branch) state=\(.state) merged_by=\(.merge_user.username // "-") merged_at=\(.merged_at // "-")"'
R "R9a2 recreate mc MR (show error if any)"
api POST /projects/$PID/merge_requests -H "Sudo: alice" -d source_branch=mc1 -d target_branch=main -d title="mc MR"
R "R21 show errors: blocked project-Owner erin via Sudo"
api POST /projects/$PID/issues -H "Sudo: erin" -d title="GH issue 77" -d iid=77 -d created_at=2018-08-08T08:08:08Z
api POST /projects/$PID/issues/1/notes -H "Sudo: erin" -d body="note by blocked owner" -d created_at=2018-08-09T00:00:00Z
R "R21b same user erin UNBLOCKED (active Owner) for comparison"
api POST /users/5/unblock >/dev/null 2>&1; ERIN=$(curl -s -H "PRIVATE-TOKEN: $TOK" "$B/users?username=erin" | jq '.[0].id'); api POST /users/$ERIN/unblock
api POST /projects/$PID/issues -H "Sudo: erin" -d title="GH issue 77" -d iid=77 -d created_at=2018-08-08T08:08:08Z | head -c 120; echo; echo "iid=$(j .iid) created_at=$(j .created_at) author=$(j .author.username)"
R "R21c erin DEACTIVATED Owner"
api POST /users/$ERIN/deactivate
api POST /projects/$PID/issues/1/notes -H "Sudo: erin" -d body="note by deactivated owner" -d created_at=2018-08-09T00:00:00Z | head -c 100; echo; echo "created_at=$(j .created_at) author=$(j .author.username)"
R "R22 show error: blocked group-Owner frank"
api POST /projects/$PID/issues/1/notes -H "Sudo: frank" -d body="x" -d created_at=2018-08-10T00:00:00Z
R "R23 show error: assign blocked/deactivated users"
api PUT /projects/$PID/merge_requests/10 -d "assignee_ids[]=$ERIN"
echo "== DONE3 =="
