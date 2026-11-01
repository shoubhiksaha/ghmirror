#!/bin/bash
set -u
B=http://localhost:8929/api/v4; TOK=glpat-roadblocktest000000001
H(){ curl -s -H "PRIVATE-TOKEN: $TOK" "$B$1"; }
P=acme%2Fnmu-import
echo "### import status"; H "/projects/$P/import" | jq -c '{import_status, import_error, stats}'
echo; echo "### issues (GitHub: #1 2026-07-12 anaranyalikestodev 2c, #2 2026-07-19 SMan16 1c, #3 2026-09-06 Feluda4u 1c)"
H "/projects/$P/issues?per_page=50&order_by=created_at&sort=asc" | jq -r '.[]|"iid=\(.iid) state=\(.state) created=\(.created_at) author=\(.author.username) notes=\(.user_notes_count)"'
echo; echo "### merge requests (GitHub: #4 merged 10rc/3rev, #5 merged 1rc/2rev, #6 merged 0/1, #7 open 0/1)"
H "/projects/$P/merge_requests?per_page=50&state=all" | jq -r '.[]|"iid=\(.iid) state=\(.state) created=\(.created_at) merged_at=\(.merged_at) author=\(.author.username) merged_by=\(.merge_user.username) notes=\(.user_notes_count) src=\(.source_branch)"'
echo; echo "### MR 4 discussions: types, authors, created_at, positions"
H "/projects/$P/merge_requests/4/discussions?per_page=100" | jq -r '.[]|.notes[]|"\(.type // "Note")\t\(.author.username)\t\(.created_at)\tsystem=\(.system)\tline=\(.position.new_line // .position.old_line // "-")\tbody=\(.body[:60]|gsub("\n";" "))"' | head -40
echo; echo "### MR 4 approvals"; H "/projects/$P/merge_requests/4/approvals" | jq -c '{approved_by:[.approved_by[].user.username]}'
echo; echo "### users created by import (placeholders / import users)"
H "/users?per_page=100" | jq -r '.[]|"\(.username)\tstate=\(.state)\tbot=\(.bot)\temail=\(.email // "-")\tname=\(.name)"' 
echo; echo "### placeholder reassignment list for group acme"
GID=$(H "/groups?search=acme" | jq '.[0].id'); H "/groups/$GID/placeholder_reassignments" | head -c 800; echo
echo; echo "### labels / branches / tags / protected branches"
echo "labels=$(H "/projects/$P/labels?per_page=100" | jq length) branches=$(H "/projects/$P/repository/branches?per_page=100" | jq length) tags=$(H "/projects/$P/repository/tags?per_page=100" | jq length) protected=$(H "/projects/$P/protected_branches" | jq -c '[.[].name]')"
echo; echo "### issue 1 notes (authors/created_at)"
H "/projects/$P/issues/1/notes" | jq -r '.[]|"\(.author.username)\t\(.created_at)\tsystem=\(.system)\t\(.body[:70]|gsub("\n";" "))"'
