"""Object-level assertions for the e2e test: GitHub fixture vs each GitLab target.

usage: assert_mirror.py phase1|phase2 CONFIG
Prints PASS/FAIL lines; exit code 1 if anything failed.
"""
import json
import os
import subprocess
import sys
import time
from urllib.parse import quote

import requests
import yaml

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
phase, cfg_path = sys.argv[1], sys.argv[2]
from ghmirror.config import _expand   # same ${VAR:-default} interpolation as the engine

cfg = _expand(yaml.safe_load(open(cfg_path).read()))
fx = json.load(open(os.path.join(STATE, "fixture.json")))
fx2 = json.load(open(os.path.join(STATE, "fixture2.json"))) if phase == "phase2" else {}
OWNER, REPO = fx["owner"], fx["repo"]
GH = requests.Session()
GH.headers.update({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"})
failures = 0


def gh(path, **params):
    out, url = [], f"https://api.github.com/repos/{OWNER}/{REPO}{path}"
    while url:
        r = GH.get(url, params=params or None)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return data
        out += data
        url, params = r.links.get("next", {}).get("url"), None
    return out


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def check(target, what, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"{'PASS' if ok else 'FAIL'} [{target}] {what}{'' if ok else '  -> ' + str(detail)}")


for t in cfg["targets"]:
    name = t["name"]
    S = requests.Session()
    S.headers["PRIVATE-TOKEN"] = t["token"]
    API = t["url"] + "/api/v4"

    def gl(path, **params):
        out, page = [], 1
        while True:
            for wait in (2, 5, 10, 20, 40, 60, 0):   # a loaded GitLab answers 502 now and then
                r = S.get(API + path, params=dict(params, per_page=100, page=page))
                if r.status_code not in (500, 502, 503, 504) or not wait:
                    break
                time.sleep(wait)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return data
            out += data
            if not r.headers.get("X-Next-Page"):
                return out
            page = int(r.headers["X-Next-Page"])

    proj = gl(f"/projects/{quote(t['group'] + '/' + REPO, safe='')}")
    check(name, "project exists", proj is not None)
    if not proj:
        continue
    pid = proj["id"]
    repo = gh("")
    check(name, "project private + description + topics",
          proj["visibility"] == "private" and proj["description"] == repo["description"]
          and sorted(proj["topics"]) == sorted(repo["topics"]), (proj["visibility"], proj["topics"]))
    check(name, "CI disabled in MIRROR mode", proj["builds_access_level"] == "disabled", proj["builds_access_level"])
    if t.get("ci_config_path"):
        check(name, "out-of-tree CI config path", proj["ci_config_path"].endswith(f"@{t['group']}/ci-config"),
              proj["ci_config_path"])
    check(name, "default branch", proj["default_branch"] == repo["default_branch"], proj["default_branch"])

    gh_br = {b["name"]: b["commit"]["sha"] for b in gh("/branches")}
    gl_br = {b["name"]: b["commit"]["id"] for b in gl(f"/projects/{pid}/repository/branches")}
    check(name, "branches identical (names + SHAs, no leftover gh-pr refs)", gh_br == gl_br,
          {"github_only": sorted(set(gh_br) - set(gl_br)), "gitlab_only": sorted(set(gl_br) - set(gh_br)),
           "sha_diff": [b for b in gh_br if b in gl_br and gh_br[b] != gl_br[b]]})
    gh_tags = {x["name"]: x["commit"]["sha"] for x in gh("/tags")}
    gl_tags = {x["name"]: x["commit"]["id"] for x in gl(f"/projects/{pid}/repository/tags")}
    check(name, "tags identical", gh_tags == gl_tags, (gh_tags, gl_tags))
    prot = gl(f"/projects/{pid}/protected_branches")
    check(name, "only the bot wildcard protection", [p["name"] for p in prot] == ["*"] and prot[0]["allow_force_push"],
          prot)

    owner = gl("/users", username=OWNER)[0]
    full_owner = gl(f"/users/{owner['id']}")
    check(name, "mirror account for owner is blocked", owner["state"] == "blocked", owner["state"])
    check(name, "mirror account email from identity map", full_owner["email"] == "shoubhik@company.test",
          full_owner["email"])
    grp = gl(f"/groups/{quote(t['group'], safe='')}")
    mem = gl(f"/groups/{grp['id']}/members/{owner['id']}")
    check(name, "mirror account is Owner of the top group", mem and mem["access_level"] == 50, mem)

    gh_labels = {lb["name"]: lb["color"] for lb in gh("/labels")}
    gl_labels = {lb["name"]: lb["color"].lstrip("#") for lb in gl(f"/projects/{pid}/labels")}
    check(name, "labels identical (names + colors)", gh_labels == gl_labels,
          {k: (gh_labels.get(k), gl_labels.get(k)) for k in set(gh_labels) | set(gl_labels) if gh_labels.get(k) != gl_labels.get(k)})
    ms = {m["title"]: m for m in gl(f"/projects/{pid}/milestones")}
    for gm in gh("/milestones", state="all"):
        m = ms.get(gm["title"])
        check(name, f"milestone {gm['title']} (description, due date, state)",
              m and m["description"] == gm["description"] and m["due_date"] == (gm["due_on"] or "")[:10] or None
              and m["state"] == ("closed" if gm["state"] == "closed" else "active"), m)

    def notes_of(noun, iid):
        return [n for n in gl(f"/projects/{pid}/{noun}/{iid}/notes", sort="asc", order_by="created_at") if not n["system"]]

    # issue numbers and every GitHub issue's core fields
    for it in [i for i in gh("/issues", state="all") if "pull_request" not in i]:
        gi = gl(f"/projects/{pid}/issues/{it['number']}")
        if not gi:
            check(name, f"issue #{it['number']} exists", False)
            continue
        check(name, f"issue #{it['number']} title/author/created_at/state",
              gi["title"] == it["title"] and gi["author"]["username"] == it["user"]["login"]
              and iso(gi["created_at"]) == iso(it["created_at"])
              and gi["state"] == ("closed" if it["state"] == "closed" else "opened"),
              (gi["title"], gi["author"]["username"], gi["created_at"], it["created_at"], gi["state"]))
        check(name, f"issue #{it['number']} labels", sorted(gi["labels"]) == sorted(lb["name"] for lb in it["labels"]),
              (gi["labels"], [lb["name"] for lb in it["labels"]]))
        if it["state"] == "closed":
            check(name, f"issue #{it['number']} closed_at exact", iso(gi["closed_at"]) == iso(it["closed_at"]),
                  (gi["closed_at"], it["closed_at"]))
        comments = gh(f"/issues/{it['number']}/comments")
        notes = notes_of("issues", it["number"])
        check(name, f"issue #{it['number']} comments -> notes (body, author, created_at)",
              [(c["body"], c["user"]["login"], iso(c["created_at"])) for c in comments] ==
              [(n["body"], n["author"]["username"], iso(n["created_at"])) for n in notes],
              ([(c["body"], iso(c["created_at"])) for c in comments], [(n["body"], iso(n["created_at"])) for n in notes]))
    i1 = fx["issue_open"]
    gi1 = gl(f"/projects/{pid}/issues/{i1}")
    check(name, "issue #1 milestone + assignee", (gi1["milestone"] or {}).get("title") == "v1.0"
          and [a["username"] for a in gi1["assignees"]] == [OWNER], gi1["assignees"])
    awards = sorted(a["name"] for a in gl(f"/projects/{pid}/issues/{i1}/award_emoji"))
    check(name, "issue #1 reactions -> award emoji", awards == ["heart", "thumbsup"], awards)
    if phase == "phase1":
        n0 = (notes_of("issues", i1) or [None])[0]
        a0 = [a["name"] for a in gl(f"/projects/{pid}/issues/{i1}/notes/{n0['id']}/award_emoji")] if n0 else []
        check(name, "comment reaction -> note award", a0 == ["eyes"], a0)

    # pull requests
    for pr in gh("/pulls", state="all"):
        n = pr["number"]
        pr = gh(f"/pulls/{n}")
        mr = gl(f"/projects/{pid}/merge_requests/{n}")
        if not mr:
            check(name, f"MR !{n} exists (number parity)", False)
            continue
        state = "merged" if pr["merged_at"] else ("closed" if pr["state"] == "closed" else "opened")
        check(name, f"MR !{n} number/title/author/created_at/state/target",
              mr["iid"] == n and mr["title"] == pr["title"] and mr["author"]["username"] == pr["user"]["login"]
              and iso(mr["created_at"]) == iso(pr["created_at"]) and mr["state"] == state
              and mr["target_branch"] == pr["base"]["ref"],
              (mr["iid"], mr["title"], mr["author"]["username"], mr["created_at"], pr["created_at"], mr["state"], state,
               mr["target_branch"]))
        if state == "merged":
            mu = mr.get("merge_user") or mr.get("merged_by") or {}
            check(name, f"MR !{n} merged_by/merged_at exact",
                  mu.get("username") == pr["merged_by"]["login"] and iso(mr["merged_at"]) == iso(pr["merged_at"]),
                  (mu.get("username"), mr["merged_at"], pr["merged_at"]))
        if state == "closed":
            check(name, f"MR !{n} closed_at exact", iso(mr["closed_at"]) == iso(pr["closed_at"]),
                  (mr["closed_at"], pr["closed_at"]))
        if state == "opened":
            check(name, f"MR !{n} source branch is the PR head branch at the PR head SHA",
                  mr["source_branch"] == pr["head"]["ref"] and mr["sha"] == pr["head"]["sha"],
                  (mr["source_branch"], mr["sha"], pr["head"]["sha"]))
        changes = gl(f"/projects/{pid}/merge_requests/{n}/changes") or {}
        gh_files = sorted(f["filename"] for f in gh(f"/pulls/{n}/files"))
        gl_files = sorted(c["new_path"] for c in changes.get("changes", []))
        check(name, f"MR !{n} diff shows the same files as the PR", gh_files == gl_files, (gh_files, gl_files))

    pm = fx["pr_merge"]
    disc = gl(f"/projects/{pid}/merge_requests/{pm}/discussions")
    diff_d = [d for d in disc if d["notes"][0]["type"] == "DiffNote"]
    rcs = gh(f"/pulls/{pm}/comments")
    tops = [c for c in rcs if not c.get("in_reply_to_id")]
    check(name, f"MR !{pm} line comments -> positioned diff discussions", len(diff_d) == len(tops),
          [(d["notes"][0]["type"], d["notes"][0]["body"][:30]) for d in disc])
    by_body = {d["notes"][0]["body"]: d for d in diff_d}
    d5 = by_body.get("Why change line 5?")
    check(name, "line-5 comment anchored at new_line 5 with reply in same thread",
          d5 and d5["notes"][0]["position"]["new_line"] == 5 and len(d5["notes"]) == 2
          and d5["notes"][1]["body"] == "Because the spec changed.", d5 and [n["body"] for n in d5["notes"]])
    dm = by_body.get("Multi-line remark on 9-10.")
    lr = dm and (dm["notes"][0]["position"].get("line_range") or {})
    check(name, "multi-line comment keeps its 9-10 range",
          dm and lr and lr["start"]["new_line"] == 9 and lr["end"]["new_line"] == 10, lr)
    dr = by_body.get("New file looks fine.")
    check(name, "resolved GitHub thread is resolved", dr and dr["notes"][0]["resolved"], dr and dr["notes"][0].get("resolved"))
    a5 = d5 and [a["name"] for a in gl(f"/projects/{pid}/merge_requests/{pm}/notes/{d5['notes'][0]['id']}/award_emoji")]
    check(name, "review comment reaction -> award", a5 == ["rocket"], a5)
    rv_notes = [x["body"] for x in notes_of("merge_requests", pm)]
    reviews = [r for r in gh(f"/pulls/{pm}/reviews") if r["body"]]
    check(name, "review summaries (incl. bot reviewer) are MR notes with their authors",
          all(r["body"] in rv_notes for r in reviews), (rv_notes, [r["body"][:40] for r in reviews]))
    bots = [r["user"]["login"] for r in reviews if r["user"]["type"] == "Bot"]
    for b in bots:
        u = gl("/users", username=b.replace("[bot]", "-bot"))
        check(name, f"bot account {b} mirrored as blocked user", u and u[0]["state"] == "blocked", u)

    rels = {r["tag_name"]: r for r in gh("/releases")}
    for tag, rel in rels.items():
        gr = gl(f"/projects/{pid}/releases/{quote(tag, safe='')}")
        check(name, f"release {tag} name/description/date",
              gr and gr["name"] == rel["name"] and gr["description"] == rel["body"]
              and iso(gr["released_at"]) == iso(rel["published_at"]), gr and (gr["name"], gr["released_at"]))
        for a in rel["assets"]:
            link = next((ln for ln in (gr or {}).get("assets", {}).get("links", []) if ln["name"] == a["name"]), None)
            body = S.get(link["url"]).content if link else b""
            check(name, f"release {tag} asset {a['name']} downloadable from GitLab with identical bytes",
                  body == b"release asset payload\n", body[:60])

    if phase == "phase2":
        po = fx["pr_open"]
        mro = gl(f"/projects/{pid}/merge_requests/{po}")
        check(name, f"previously open MR !{po} now merged (squash) after sync", mro["state"] == "merged", mro["state"])
        late = gl(f"/projects/{pid}/merge_requests/{fx2['pr_late']}")
        check(name, "new PR got its exact number as MR iid", late and late["iid"] == fx2["pr_late"], late and late["iid"])
        newi = gl(f"/projects/{pid}/issues/{fx2['issue_new']}")
        check(name, "new issue got its exact number", newi and newi["iid"] == fx2["issue_new"])
        bare = os.path.join(os.environ["E2E_STATE"], "mirrors", OWNER, REPO + ".git")
        backups = subprocess.run(["git", "for-each-ref", "refs/ghmirror-backup/"], cwd=bare, capture_output=True,
                                 text=True).stdout
        check(name, "force-pushed/deleted branch tips preserved in the bare mirror",
              "heads/feat-rebase" in backups and "heads/feat-open" in backups, backups)

print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
