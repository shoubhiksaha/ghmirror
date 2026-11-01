"""Builds a scratch GitHub repository full of the objects the mirror must copy.

phase1: history, branches, tags, labels, milestone, issues, comments, reactions,
        PRs merged by merge/squash/rebase, a closed PR, an open PR, line review
        comments with a reply and a resolved thread, a release with an asset.
phase2: changes made after the first sync (edits, deletes, new objects, a merge
        of the open PR, a force push) to test incremental sync.
doomed: a second repo that is mirrored and then deleted on GitHub.

Usage: python github_fixture.py phase1|phase2|doomed-create|doomed-delete OWNER REPO
Token comes from GITHUB_TOKEN.
"""
import json, os, subprocess, sys, tempfile, time
import requests

API = "https://api.github.com"
S = requests.Session()
S.headers.update({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                  "Accept": "application/vnd.github+json",
                  "X-GitHub-Api-Version": "2022-11-28"})


def gh(method, path, **kw):
    url = path if path.startswith("http") else API + path
    for attempt in range(6):
        r = S.request(method, url, **kw)
        if r.status_code in (502, 503) or (r.status_code == 422 and "not mergeable" in r.text.lower()) \
                or (r.status_code == 405 and "Base branch was modified" in r.text):
            time.sleep(3 + attempt * 2)
            continue
        break
    if r.status_code >= 400:
        raise SystemExit(f"{method} {path} -> {r.status_code} {r.text[:400]}")
    return r.json() if r.text else None


def git(cwd, *args, env=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def git_env():
    e = dict(os.environ)
    e.update({"GIT_AUTHOR_NAME": "Mirror Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.com",
              "GIT_COMMITTER_NAME": "Mirror Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.com",
              "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
              "GIT_CONFIG_VALUE_0": "Authorization: Basic " + __import__("base64").b64encode(
                  f"x-access-token:{os.environ['GITHUB_TOKEN']}".encode()).decode()})
    return e


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def wait_mergeable(owner, repo, n):
    for _ in range(20):
        pr = gh("GET", f"/repos/{owner}/{repo}/pulls/{n}")
        if pr["mergeable"] is not None:
            return pr
        time.sleep(2)
    return pr


def phase1(owner, repo):
    gh("POST", "/user/repos", json={"name": repo, "private": True, "has_wiki": True,
                                     "description": "ghmirror end-to-end fixture (safe to delete)"})
    gh("PUT", f"/repos/{owner}/{repo}/topics", json={"names": ["mirror-test", "fixture"]})
    env = git_env()
    d = tempfile.mkdtemp(prefix="ghfix-")
    git(d, "init", "-q", "-b", "main", env=env)
    write(f"{d}/app.txt", "".join(f"line {i}\n" for i in range(1, 21)))
    write(f"{d}/README.md", "# fixture\n")
    git(d, "add", "."); git(d, "commit", "-qm", "initial commit", env=env)
    write(f"{d}/notes.txt", "notes v1\n"); git(d, "add", "."); git(d, "commit", "-qm", "add notes", env=env)
    git(d, "tag", "-a", "v0.1", "-m", "first tag", env=env)
    for b, fname, content in [("feat-merge", "merge.txt", "merged via merge commit\n"),
                              ("feat-squash", "squash.txt", "merged via squash\n"),
                              ("feat-rebase", "rebase.txt", "merged via rebase\n"),
                              ("feat-closed", "closed.txt", "never merged\n"),
                              ("feat-open", "open.txt", "still open\n")]:
        git(d, "checkout", "-q", "-b", b, "main")
        write(f"{d}/{fname}", content); git(d, "add", "."); git(d, "commit", "-qm", f"{b}: add {fname}", env=env)
        if b == "feat-merge":
            lines = open(f"{d}/app.txt").read().splitlines()
            lines[4] = "line 5 changed in feat-merge"; lines[9] = "line 10 changed in feat-merge"
            write(f"{d}/app.txt", "\n".join(lines) + "\n")
            git(d, "commit", "-qam", "feat-merge: edit app.txt", env=env)
        if b == "feat-squash":
            write(f"{d}/{fname}", content + "second commit\n"); git(d, "commit", "-qam", "feat-squash: second", env=env)
    git(d, "checkout", "-q", "main")
    git(d, "remote", "add", "origin", f"https://github.com/{owner}/{repo}.git")
    git(d, "push", "-q", "origin", "--all", env=env); git(d, "push", "-q", "origin", "--tags", env=env)

    R = f"/repos/{owner}/{repo}"
    gh("POST", f"{R}/labels", json={"name": "mirror-test", "color": "5319e7", "description": "created by fixture"})
    ms = gh("POST", f"{R}/milestones", json={"title": "v1.0", "description": "first milestone",
                                               "due_on": "2026-12-31T00:00:00Z"})
    i1 = gh("POST", f"{R}/issues", json={"title": "Crash on startup", "body": "Steps:\n1. run\n2. crash\n\nSee app.txt",
                                          "labels": ["bug", "mirror-test"], "milestone": ms["number"],
                                          "assignees": [owner]})
    c1 = gh("POST", f"{R}/issues/{i1['number']}/comments", json={"body": "Reproduced on main."})
    gh("POST", f"{R}/issues/{i1['number']}/comments", json={"body": "Probably the config loader."})
    gh("POST", f"{R}/issues/{i1['number']}/reactions", json={"content": "+1"})
    gh("POST", f"{R}/issues/{i1['number']}/reactions", json={"content": "heart"})
    gh("POST", f"{R}/issues/comments/{c1['id']}/reactions", json={"content": "eyes"})
    i2 = gh("POST", f"{R}/issues", json={"title": "Docs typo", "body": "README says fixtrue", "labels": ["documentation"]})
    gh("POST", f"{R}/issues/{i2['number']}/comments", json={"body": "Fixed, closing."})
    gh("PATCH", f"{R}/issues/{i2['number']}", json={"state": "closed", "state_reason": "completed"})

    prs = {}
    for b, title in [("feat-merge", "Merge-commit PR"), ("feat-squash", "Squash PR"), ("feat-rebase", "Rebase PR"),
                     ("feat-closed", "Closed PR"), ("feat-open", "Open PR")]:
        prs[b] = gh("POST", f"{R}/pulls", json={"title": title, "head": b, "base": "main",
                                                 "body": f"Body of {title}.\n\nRefs #{i1['number']}"})
    pm = prs["feat-merge"]
    gh("PATCH", f"{R}/issues/{pm['number']}", json={"labels": ["mirror-test"], "milestone": ms["number"]})
    gh("POST", f"{R}/issues/{pm['number']}/comments", json={"body": "Conversation comment on the PR."})
    head = pm["head"]["sha"]
    rv = gh("POST", f"{R}/pulls/{pm['number']}/reviews", json={
        "commit_id": head, "event": "COMMENT", "body": "Review summary: two remarks.",
        "comments": [{"path": "app.txt", "line": 5, "side": "RIGHT", "body": "Why change line 5?"},
                     {"path": "app.txt", "start_line": 9, "start_side": "RIGHT", "line": 10, "side": "RIGHT",
                      "body": "Multi-line remark on 9-10."},
                     {"path": "merge.txt", "line": 1, "side": "RIGHT", "body": "New file looks fine."}]})
    rcs = gh("GET", f"{R}/pulls/{pm['number']}/reviews/{rv['id']}/comments")
    first = [c for c in rcs if c["body"].startswith("Why")][0]
    gh("POST", f"{R}/pulls/{pm['number']}/comments/{first['id']}/replies", json={"body": "Because the spec changed."})
    gh("POST", f"{R}/pulls/comments/{first['id']}/reactions", json={"content": "rocket"})
    # resolve the thread on merge.txt
    q = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){
           reviewThreads(first:50){nodes{id isResolved comments(first:1){nodes{body}}}}}}}"""
    th = gh("POST", "/graphql", json={"query": q, "variables": {"o": owner, "r": repo, "n": pm["number"]}})
    for t in th["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
        if t["comments"]["nodes"][0]["body"].startswith("New file"):
            gh("POST", "/graphql", json={"query": "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}",
                                         "variables": {"id": t["id"]}})
    for b, method in [("feat-merge", "merge"), ("feat-squash", "squash"), ("feat-rebase", "rebase")]:
        wait_mergeable(owner, repo, prs[b]["number"])
        gh("PUT", f"{R}/pulls/{prs[b]['number']}/merge", json={"merge_method": method})
        time.sleep(2)
    gh("DELETE", f"{R}/git/refs/heads/feat-squash")  # like "auto-delete head branches"
    gh("PATCH", f"{R}/pulls/{prs['feat-closed']['number']}", json={"state": "closed"})
    gh("POST", f"{R}/issues/{prs['feat-open']['number']}/comments", json={"body": "Waiting for review."})

    rel = gh("POST", f"{R}/releases", json={"tag_name": "v1.0", "target_commitish": "main", "name": "Version 1.0",
                                             "body": "First release.\n\n- merge\n- squash\n- rebase"})
    up = rel["upload_url"].split("{")[0]
    gh("POST", f"{up}?name=fixture-asset.txt", data=b"release asset payload\n",
       headers={"Content-Type": "text/plain"})
    out = {"owner": owner, "repo": repo, "issue_open": i1["number"], "issue_closed": i2["number"],
           "milestone": ms["number"], **{f"pr_{k.split('-')[1]}": v["number"] for k, v in prs.items()}}
    print(json.dumps(out))


def phase2(owner, repo):
    R = f"/repos/{owner}/{repo}"
    st = json.load(open(os.path.join(os.path.dirname(__file__), "state", "fixture.json")))
    env = git_env()
    d = tempfile.mkdtemp(prefix="ghfix2-")
    git(d, "clone", "-q", f"https://github.com/{owner}/{repo}.git", ".", env=env)
    # edit + close an issue, add a label, edit and delete comments
    gh("PATCH", f"{R}/issues/{st['issue_open']}", json={"title": "Crash on startup (config loader)",
                                                         "labels": ["bug", "mirror-test", "enhancement"]})
    comments = gh("GET", f"{R}/issues/{st['issue_open']}/comments")
    gh("PATCH", f"{R}/issues/comments/{comments[0]['id']}", json={"body": "Reproduced on main (edited)."})
    gh("DELETE", f"{R}/issues/comments/{comments[1]['id']}")
    gh("POST", f"{R}/issues/{st['issue_open']}/comments", json={"body": "Fix is in the open PR."})
    gh("PATCH", f"{R}/issues/{st['issue_open']}", json={"state": "closed", "state_reason": "completed"})
    i3 = gh("POST", f"{R}/issues", json={"title": "New issue after first sync", "body": "Created in phase 2.",
                                          "labels": ["mirror-test"]})
    # new same-repo PR, then a second push to it
    git(d, "checkout", "-q", "-b", "feat-late", "origin/main")
    write(f"{d}/late.txt", "late feature\n"); git(d, "add", "."); git(d, "commit", "-qm", "feat-late: add", env=env)
    git(d, "push", "-q", "origin", "feat-late", env=env)
    pl = gh("POST", f"{R}/pulls", json={"title": "Late PR", "head": "feat-late", "base": "main", "body": "Opened in phase 2."})
    write(f"{d}/late.txt", "late feature\nmore\n"); git(d, "commit", "-qam", "feat-late: more", env=env)
    git(d, "push", "-q", "origin", "feat-late", env=env)
    # merge the previously-open PR by squash and delete its branch
    wait_mergeable(owner, repo, st["pr_open"])
    gh("PUT", f"{R}/pulls/{st['pr_open']}/merge", json={"merge_method": "squash"})
    gh("DELETE", f"{R}/git/refs/heads/feat-open")
    # force-push: rewrite feat-rebase's branch tip
    git(d, "checkout", "-q", "-B", "feat-rebase", "origin/main")
    write(f"{d}/forced.txt", "force pushed\n"); git(d, "add", "."); git(d, "commit", "-qm", "forced commit", env=env)
    git(d, "push", "-q", "-f", "origin", "feat-rebase", env=env)
    # new tag + release
    git(d, "fetch", "-q", "origin", env=env); git(d, "tag", "v1.1", "origin/main")
    git(d, "push", "-q", "origin", "v1.1", env=env)
    gh("POST", f"{R}/releases", json={"tag_name": "v1.1", "name": "Version 1.1", "body": "Second release."})
    gh("POST", f"{R}/labels", json={"name": "late-label", "color": "0e8a16"})
    print(json.dumps({"issue_new": i3["number"], "pr_late": pl["number"]}))


def doomed_create(owner, repo):
    gh("POST", "/user/repos", json={"name": repo, "private": True, "auto_init": True,
                                     "description": "ghmirror deletion-guard fixture"})
    time.sleep(2)
    gh("POST", f"/repos/{owner}/{repo}/issues", json={"title": "doomed issue", "body": "will be deleted with the repo"})
    print(json.dumps({"doomed": repo}))


if __name__ == "__main__":
    cmd, owner, repo = sys.argv[1:4]
    {"phase1": phase1, "phase2": phase2, "doomed-create": doomed_create,
     "doomed-delete": lambda o, r: gh("DELETE", f"/repos/{o}/{r}")}[cmd](owner, repo)
