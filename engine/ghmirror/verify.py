"""Parity report: GitHub vs every target. Drift is a hard alert; --repair forces a re-sync of drifted objects."""
import json
import logging
import os
import time

from . import mapping
from .gitlab import GLNotFound
from .gitmirror import MANAGED_PREFIXES, run

log = logging.getLogger("ghmirror.verify")
SEED_GRACE = 86400   # a repository not copied yet (new, or first copy still queued) is "pending" this long


def _gh_refs(eng, full):
    p = run(["ls-remote", "--heads", "--tags", eng.gh.clone_url(full)], env=eng.gh.git_env())
    return {ref: sha for sha, ref in (ln.split("\t") for ln in p.stdout.splitlines()) if not ref.endswith("^{}")}


def verify(eng, only=None, repair=False):
    eng.prepare()
    db = eng.db
    drift = []
    checked = {"repos": 0, "issues": 0, "mrs": 0, "pending": 0}
    repos = [r for r in eng.gh.repos() if eng.selected(r) and (not only or r["full_name"] in only or r["name"] in only)]
    targets = [c for c in eng.ctxs if c.group_id and (c.mode == "mirror")]

    def d(ctx, repo, kind, ident, field, gh, gl):
        drift.append({"target": ctx.name, "repo": repo, "kind": kind, "id": ident, "field": field,
                      "github": gh, "gitlab": gl})

    for repo in repos:
        full = repo["full_name"]
        checked["repos"] += 1
        gh_refs = _gh_refs(eng, full)
        items = eng.gh.issues_since(full)
        issues = {i["number"]: i for i in items if "pull_request" not in i}
        pulls = {i["number"]: i for i in items if "pull_request" in i}
        labels = {lb["name"] for lb in eng.gh.labels(full)}
        milestones = {m["title"] for m in eng.gh.milestones(full)}
        releases = {r["tag_name"] for r in eng.gh.releases(full) if not r.get("draft")}
        for ctx in targets:
            m = db.map_get(ctx.name, "repo", str(repo["id"]))
            if not m:
                key = f"unseeded:{ctx.name}:{repo['id']}"
                first = db.get(key)
                if first is None:
                    db.set(key, time.time())
                    first = time.time()
                if time.time() - first < SEED_GRACE:
                    checked["pending"] += 1
                    continue
                d(ctx, full, "repo", full, "exists", True, False)
                continue
            pid = int(m["gl_id"])
            gl = ctx.gl
            usernames = {}

            def uname(login):
                if login not in usernames:
                    r = db.row("SELECT extra FROM mapping WHERE target=? AND kind='user' AND extra LIKE ?",
                               (ctx.name, f'%"login": "{login}"%'))
                    usernames[login] = json.loads(r["extra"])["username"] if r else mapping.username_for(login)
                return usernames[login]

            gl_refs = {k: v for k, v in eng.mirror.remote_refs(gl.git_url(m["extra"]["path"]), gl.git_env()).items()
                       if not k.startswith(MANAGED_PREFIXES)}
            for ref in sorted(set(gh_refs) | set(gl_refs)):
                if gh_refs.get(ref) != gl_refs.get(ref):
                    d(ctx, full, "ref", ref, "sha", gh_refs.get(ref), gl_refs.get(ref))
            gl_issues = {i["iid"]: i for i in gl.paginate(f"/projects/{pid}/issues", {"scope": "all"})}
            for n, it in issues.items():
                checked["issues"] += 1
                gi = gl_issues.get(n)
                if not gi:
                    d(ctx, full, "issue", n, "exists", True, False)
                    continue
                want = {"title": it["title"], "state": "closed" if it["state"] == "closed" else "opened",
                        "author": uname(it["user"]["login"]), "created_at": mapping.iso(it["created_at"]),
                        "labels": sorted(lb["name"] for lb in it["labels"]), "notes": it["comments"],
                        "milestone": (it.get("milestone") or {}).get("title")}
                have = {"title": gi["title"], "state": gi["state"], "author": gi["author"]["username"],
                        "created_at": mapping.iso(gi["created_at"]), "labels": sorted(gi["labels"]),
                        "notes": gi["user_notes_count"], "milestone": (gi.get("milestone") or {}).get("title")}
                for f in want:
                    if want[f] != have[f]:
                        d(ctx, full, "issue", n, f, want[f], have[f])
            # the other direction: something on the mirror that GitHub does not have (created by hand on a
            # mirror, a duplicate, a drill leftover). Reported, never deleted. Issues deleted on GitHub are
            # kept on purpose, closed and labelled, so they do not count.
            for n, gi in gl_issues.items():
                if n not in issues and mapping.DELETED_LABEL not in gi["labels"]:
                    d(ctx, full, "issue", n, "exists", False, True)
            gl_mrs = {x["iid"]: x for x in gl.paginate(f"/projects/{pid}/merge_requests", {"scope": "all"})}
            for n, mr in gl_mrs.items():
                if n not in pulls and mapping.DELETED_LABEL not in mr["labels"]:
                    d(ctx, full, "mr", n, "exists", False, True)
            for n, it in pulls.items():
                checked["mrs"] += 1
                mr = gl_mrs.get(n)
                if not mr:
                    d(ctx, full, "mr", n, "exists", True, False)
                    continue
                pr = eng.gh.pull(full, n)
                reviews = eng.gh.reviews(full, n)
                review_notes = sum(1 for r in reviews if mapping.review_note_body(r) is not None)
                state = mapping.pr_state(pr)
                want = {"title": mapping.mr_title(pr), "state": state, "author": uname(pr["user"]["login"]),
                        "created_at": mapping.iso(pr["created_at"]), "target_branch": pr["base"]["ref"],
                        "notes": it["comments"] + pr.get("review_comments", 0) + review_notes,
                        "labels": sorted(lb["name"] for lb in pr["labels"])}
                have = {"title": mr["title"], "state": mr["state"], "author": mr["author"]["username"],
                        "created_at": mapping.iso(mr["created_at"]), "target_branch": mr["target_branch"],
                        "notes": mr["user_notes_count"], "labels": sorted(mr["labels"])}
                if state == "merged":
                    want.update(merged_by=uname(pr["merged_by"]["login"]) if pr.get("merged_by") else None,
                                merged_at=mapping.iso(pr["merged_at"]))
                    mu = mr.get("merge_user") or mr.get("merged_by") or {}
                    have.update(merged_by=mu.get("username"), merged_at=mapping.iso(mr.get("merged_at")))
                for f in want:
                    if want[f] != have[f]:
                        d(ctx, full, "mr", n, f, want[f], have[f])
            gl_labels = {lb["name"] for lb in gl.paginate(f"/projects/{pid}/labels", {"include_ancestor_groups": "false"})}
            if labels != gl_labels:
                d(ctx, full, "labels", "-", "set", sorted(labels - gl_labels), sorted(gl_labels - labels))
            gl_ms = {x["title"] for x in gl.paginate(f"/projects/{pid}/milestones")}
            if milestones != gl_ms:
                d(ctx, full, "milestones", "-", "set", sorted(milestones - gl_ms), sorted(gl_ms - milestones))
            gl_rel = {x["tag_name"] for x in gl.paginate(f"/projects/{pid}/releases")}
            if releases != gl_rel:
                d(ctx, full, "releases", "-", "set", sorted(releases - gl_rel), sorted(gl_rel - releases))
            if repair:
                for x in drift:
                    if x["target"] == ctx.name and x["repo"] == full and x["kind"] in ("issue", "mr"):
                        db.execute("UPDATE mapping SET hash=NULL, gh_updated=NULL WHERE target=? AND kind=? "
                                   "AND parent=? AND gl_iid=?", (ctx.name, x["kind"], str(repo["id"]), x["id"]))
                if any(x["target"] == ctx.name and x["repo"] == full for x in drift):
                    db.enqueue("full", full)
    report = {"ts": time.time(), "checked": checked, "drift_count": len(drift), "drift": drift}
    out = os.path.join(eng.archive.root, "_reports")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, f"verify-{time.strftime('%Y%m%dT%H%M%S')}.json"), "w") as f:
        json.dump(report, f, indent=1)
    db.set("last_verify", {"ts": report["ts"], "checked": checked, "drift_count": len(drift)})
    if drift:
        eng.alert("error", f"parity check found {len(drift)} differences, e.g. {drift[:3]}")
    return report
