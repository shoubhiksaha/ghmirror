"""L3 sync of one repository: GitHub objects -> GitLab objects on every active target.

Everything is idempotent: each write first consults the mapping table (target, kind, github id),
then tries to adopt an existing GitLab object (same iid / name / author+created_at), and only then
creates. Content hashes skip unchanged objects. Fields the API cannot set are queued on the
target's side channel and applied in one Rails run at the end of the pass.
"""
import logging
import mimetypes
import os
import time
from datetime import datetime, timezone

from . import mapping
from .gitlab import MAINTAINER, GLError, GLNotFound, enc
from .gitmirror import GitError

log = logging.getLogger("ghmirror.sync")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ObjectFailed(Exception):
    pass


class RepoSync:
    def __init__(self, eng, repo):
        self.e = eng
        self.db = eng.db
        self.gh = eng.gh
        self.repo = repo
        self.full = repo["full_name"]
        self.owner, self.name = self.full.split("/")
        self.key = str(repo["id"])
        self.ctxs = eng.writable_targets()
        self.projects = {}
        self.milestones = {}   # ctx.name -> {github milestone number: gitlab milestone id}
        self.post_flush = {}   # ctx.name -> [(op index, callback)]
        self.sc_ops = {}       # ctx.name -> side-channel operations queued during this pass
        self.mr_create_stop = None   # first PR number whose MR could not be created in this pass
        self.mirror_path = eng.mirror.path(self.full)
        self.failures = []

    # ------------------------------------------------------------------ projects
    def ensure_projects(self):
        for ctx in list(self.ctxs):
            try:
                self.projects[ctx.name] = self._ensure_project(ctx)
            except Exception as e:
                log.exception("[%s] project for %s failed", ctx.name, self.full)
                self.e.target_failed(ctx, e)
                self.ctxs.remove(ctx)

    def _project_fields(self, ctx):
        r = self.repo
        f = {"name": r["name"], "description": (r.get("description") or "")[:2000],
             "visibility": mapping.visibility(r, ctx.t.visibility_cap), "topics": sorted(r.get("topics") or []),
             "issues_access_level": "enabled" if r.get("has_issues", True) else "disabled",
             "wiki_access_level": "enabled" if r.get("has_wiki", True) else "disabled"}
        if ctx.t.ci_config_path:
            f["ci_config_path"] = ctx.t.ci_config_path.format(repo=mapping.project_path(r["name"]), group=ctx.t.group,
                                                              owner=self.owner)
        return f

    def _ensure_project(self, ctx):
        gl = ctx.gl
        path = mapping.project_path(self.repo["name"])
        fields = self._project_fields(ctx)
        m = self.db.map_get(ctx.name, "repo", self.key)
        p = None
        if m:
            p = gl.get(f"/projects/{m['gl_id']}", ok404=True)
            if p is None:
                self.e.alert("error", f"[{ctx.name}] project for {self.full} (id {m['gl_id']}) vanished on GitLab; "
                                      f"recreating it")
                for kind in ("issue", "mr", "note", "review", "rc", "award", "label", "milestone", "release",
                             "asset", "attachment"):
                    self.db.execute("DELETE FROM mapping WHERE target=? AND kind=? AND (parent LIKE ? OR parent=?)",
                                    (ctx.name, kind, f"%{self.key}%", self.key))
        if p is None:
            p = gl.project(f"{ctx.t.group}/{path}")
        if p is None:
            data = dict(fields, path=path, namespace_id=ctx.group_id, initialize_with_readme=False,
                        builds_access_level="disabled" if ctx.mode == "mirror" else "enabled",
                        merge_requests_access_level="enabled", packages_enabled=True, auto_devops_enabled=False)
            p = gl.post("/projects", json=data, recover=lambda: gl.project(f"{ctx.t.group}/{path}"))
            self.db.audit(ctx.name, "project.create", {"repo": self.full, "id": p["id"]})
            log.info("[%s] created project %s", ctx.name, p["path_with_namespace"])
        h = mapping.digest([fields, path])
        if not m or m.get("hash") != h or m["extra"].get("full_name") != self.full:
            upd = dict(fields)
            if p["path"] != path:
                upd["path"] = path   # repository renamed on GitHub (GitLab keeps a redirect)
            if p.get("archived"):
                gl.post(f"/projects/{p['id']}/unarchive")
            gl.put(f"/projects/{p['id']}", json=upd)
            p = gl.get(f"/projects/{p['id']}")
        elif p.get("archived") and not self.repo.get("archived"):
            gl.post(f"/projects/{p['id']}/unarchive")
            p["archived"] = False
        self.db.map_put(ctx.name, "repo", self.key, p["id"], extra={
            "full_name": self.full, "path": p["path_with_namespace"], "default_branch": self.repo.get("default_branch"),
            "deleted_on_source": False}, hash=h, gh_updated=self.repo.get("updated_at"))
        return p

    def pid(self, ctx):
        return self.projects[ctx.name]["id"]

    # ------------------------------------------------------------------ git (L1)
    def sync_git(self):
        path = self.e.mirror.fetch(self.full)
        wiki = self.e.mirror.fetch(self.full, wiki=True) if self.repo.get("has_wiki") else None
        self.e.archive.put(self.full, "repo", "repo", self.repo)
        default = self.repo.get("default_branch")
        for ctx in list(self.ctxs):
            p = self.projects[ctx.name]
            try:
                if p.get("archived"):
                    ctx.gl.post(f"/projects/{p['id']}/unarchive")
                    p["archived"] = False
                if ctx.mode == "mirror":
                    self.ensure_bot_protection(ctx, p["id"])
                url = ctx.gl.git_url(p["path_with_namespace"])
                res = self.e.mirror.push(path, url, ctx.gl.git_env(), default_branch=default,
                                         delete_branch=self._branch_deleter(ctx))
                if res["errors"]:
                    raise GitError(f"push rejected: {res['errors'][:5]}")
                if default and p.get("default_branch") != default and \
                        self.e.mirror.ref_sha(path, f"refs/heads/{default}"):
                    ctx.gl.put(f"/projects/{p['id']}", json={"default_branch": default})
                    p["default_branch"] = default
                if ctx.mode == "mirror":
                    self.ensure_bot_protection(ctx, p["id"])
                if wiki:
                    try:
                        self.e.mirror.push(wiki, ctx.gl.git_url(p["path_with_namespace"], wiki=True), ctx.gl.git_env())
                    except GitError as e:
                        log.warning("[%s] wiki push for %s failed: %s", ctx.name, self.full, e)
                self.db.set(f"lastgit:{ctx.name}:{self.key}", time.time())
                if res["updated"] or res["deleted"]:
                    self.db.audit(ctx.name, "git.push", {"repo": self.full, **res})
            except Exception as e:
                log.exception("[%s] git sync of %s failed", ctx.name, self.full)
                self.e.target_failed(ctx, e)
                self.ctxs.remove(ctx)

    def _branch_deleter(self, ctx):
        pid = self.pid(ctx)
        return lambda b: ctx.gl.delete(f"/projects/{pid}/repository/branches/{enc(b)}", ok404=True)

    def ensure_bot_protection(self, ctx, pid):
        """MIRROR mode: one wildcard rule (maintainers, force-push allowed); ghosts are blocked, so only the bot writes."""
        rules = ctx.gl.get(f"/projects/{pid}/protected_branches")
        ok = False
        for r in rules:
            if r["name"] == "*" and r.get("allow_force_push"):
                ok = True
            else:
                ctx.gl.delete(f"/projects/{pid}/protected_branches/{enc(r['name'])}", ok404=True)
        if not ok:
            ctx.gl.post(f"/projects/{pid}/protected_branches", json={
                "name": "*", "push_access_level": MAINTAINER, "merge_access_level": MAINTAINER,
                "allow_force_push": True})

    # ------------------------------------------------------------------ metadata (L3)
    def sync_meta(self, full=False):
        if not self.ctxs:
            return
        cursors = [self.db.get(f"cursor:{c.name}:{self.key}") for c in self.ctxs]
        since = None if full or any(c is None for c in cursors) else min(cursors)
        labels = self.gh.labels(self.full)
        self.e.archive.put(self.full, "labels", "all", labels)
        milestones = self.gh.milestones(self.full)
        self.e.archive.put(self.full, "milestones", "all", milestones)
        for ctx in list(self.ctxs):
            self._guarded(ctx, "labels", self._labels_on, ctx, labels)
            self._guarded(ctx, "milestones", self._milestones_on, ctx, milestones)
        items = self.gh.issues_since(self.full, since)
        if since is None:
            self.e.guard.check_objects(self, items)
        items.sort(key=lambda it: it["number"])
        newest = since
        for it in items:
            if self.e.guard.halted():
                raise RuntimeError("guard halted")
            try:
                if "pull_request" in it:
                    self.sync_pr(it, force=full)
                else:
                    self.sync_issue(it, force=full)
            except Exception as e:
                log.exception("%s #%s failed", self.full, it["number"])
                self.failures.append((it["number"], str(e)))
            if not newest or it["updated_at"] > newest:
                newest = it["updated_at"]
        releases = self.gh.releases(self.full)
        for ctx in list(self.ctxs):
            self._guarded(ctx, "releases", self._releases_on, ctx, releases)
        if full:
            self.archive_settings()
        for ctx in list(self.ctxs):
            self._flush(ctx)
            self._finalize_archived(ctx)
        blocking = [f for f in self.failures if not self._failure_exhausted(f)]
        for ctx in self.ctxs:
            if not blocking and newest:
                self.db.set(f"cursor:{ctx.name}:{self.key}", newest)
            self.db.set(f"lastmeta:{ctx.name}:{self.key}", time.time())
        if self.failures:
            raise ObjectFailed(f"{self.full}: {len(self.failures)} objects failed: {self.failures[:3]}")

    def _failure_exhausted(self, failure):
        number, err = failure
        k = f"objfail:{self.key}:{number}"
        n = (self.db.get(k) or 0) + 1
        self.db.set(k, n)
        if n >= 5:
            self.e.alert("error", f"{self.full}#{number} failed {n} times, skipping it until the next full pass: {err}")
            return True
        return False

    def _guarded(self, ctx, what, fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            log.exception("[%s] %s of %s failed", ctx.name, what, self.full)
            self.failures.append((what, str(e)))

    def _finalize_archived(self, ctx):
        p = self.projects.get(ctx.name)
        if p and self.repo.get("archived") and not p.get("archived"):
            ctx.gl.post(f"/projects/{p['id']}/archive")
            p["archived"] = True

    def archive_settings(self):
        a = self.e.archive
        a.put(self.full, "settings", "branch_protection", self.gh.branch_protections(self.full))
        a.put(self.full, "settings", "rulesets", self.gh.rulesets(self.full))
        collabs = self.gh.collaborators(self.full)
        a.put(self.full, "settings", "collaborators", collabs)
        for ctx in self.ctxs:
            desired = {}
            for c in collabs:
                role = mapping.ROLE.get(c.get("role_name") or "", None)
                if role is None and c.get("permissions"):
                    perms = c["permissions"]
                    role = mapping.ROLE["admin" if perms.get("admin") else "maintain" if perms.get("maintain")
                                        else "push" if perms.get("push") else "triage" if perms.get("triage") else "pull"]
                if role:
                    desired[str(self.e.ids.ensure(ctx, c))] = role
            self.db.set(f"desired_project_members:{ctx.name}:{self.pid(ctx)}", desired)

    def sc_add(self, ctx, op, **fields):
        fields["op"] = op
        ops = self.sc_ops.setdefault(ctx.name, [])
        ops.append(fields)
        return len(ops) - 1

    def _flush(self, ctx):
        callbacks = self.post_flush.pop(ctx.name, [])
        try:
            results = ctx.sc.run(self.sc_ops.pop(ctx.name, []))
        except Exception as e:
            log.exception("[%s] side channel failed", ctx.name)
            self.failures.append(("sidechannel", str(e)))
            return
        ok = {r["i"] for r in results if r.get("ok")}
        for r in results:
            if not r.get("ok"):
                self.failures.append(("sidechannel", r.get("error")))
        for i, cb in callbacks:
            if i in ok:
                try:
                    cb()
                except Exception as e:
                    log.exception("[%s] post-flush step failed", ctx.name)
                    self.failures.append(("postflush", str(e)))

    # ------------------------------------------------------------------ labels & milestones
    def _labels_on(self, ctx, labels):
        gl, pid = ctx.gl, self.pid(ctx)
        existing = self.db.map_children(ctx.name, "label", self.key)
        current = None
        seen = set()
        for lb in labels:
            k = str(lb["id"])
            seen.add(k)
            fields = {"name": lb["name"], "color": mapping.label_color(lb.get("color")),
                      "description": lb.get("description") or ""}
            h = mapping.digest(fields)
            row = existing.get(k)
            if row and row["hash"] == h:
                continue
            if row:
                gl.put(f"/projects/{pid}/labels/{row['gl_id']}", json={"new_name": fields["name"],
                                                                       "color": fields["color"],
                                                                       "description": fields["description"]})
                gid = row["gl_id"]
            else:
                if current is None:
                    current = {x["name"]: x for x in gl.paginate(f"/projects/{pid}/labels", {"include_ancestor_groups": "false"})}
                if lb["name"] in current:
                    gid = current[lb["name"]]["id"]
                    gl.put(f"/projects/{pid}/labels/{gid}", json={"color": fields["color"],
                                                                  "description": fields["description"]})
                else:
                    gid = gl.post(f"/projects/{pid}/labels", json=fields, recover=lambda: next(
                        (x for x in gl.paginate(f"/projects/{pid}/labels", {"include_ancestor_groups": "false"})
                         if x["name"] == lb["name"]), None))["id"]
            self.db.map_put(ctx.name, "label", k, gid, parent=self.key, hash=h)
        for k, row in existing.items():
            if k not in seen:
                gl.delete(f"/projects/{pid}/labels/{row['gl_id']}", ok404=True)
                self.db.map_delete(ctx.name, "label", k)

    def _milestones_on(self, ctx, milestones):
        gl, pid = ctx.gl, self.pid(ctx)
        existing = self.db.map_children(ctx.name, "milestone", self.key)
        current = None
        ids = {}
        seen = set()
        for ms in milestones:
            k = str(ms["id"])
            seen.add(k)
            fields = {"title": ms["title"], "description": ms.get("description") or "",
                      "due_date": (ms.get("due_on") or "")[:10] or None}
            h = mapping.digest([fields, ms["state"]])
            row = existing.get(k)
            if row and row["hash"] == h:
                ids[ms["number"]] = int(row["gl_id"])
                continue
            if row:
                gid = int(row["gl_id"])
            else:
                if current is None:
                    current = {x["title"]: x for x in gl.paginate(f"/projects/{pid}/milestones")}
                gid = current[ms["title"]]["id"] if ms["title"] in current else \
                    gl.post(f"/projects/{pid}/milestones", json=fields, recover=lambda: next(
                        (x for x in gl.paginate(f"/projects/{pid}/milestones", {"title": ms["title"]})
                         if x["title"] == ms["title"]), None))["id"]
            gl.put(f"/projects/{pid}/milestones/{gid}", json=dict(
                fields, state_event="close" if ms["state"] == "closed" else "activate"))
            self.db.map_put(ctx.name, "milestone", k, gid, parent=self.key, hash=h, extra={"number": ms["number"]})
            ids[ms["number"]] = gid
        for k, row in existing.items():
            if k not in seen:
                gl.delete(f"/projects/{pid}/milestones/{row['gl_id']}", ok404=True)
                self.db.map_delete(ctx.name, "milestone", k)
        self.milestones[ctx.name] = ids

    # ------------------------------------------------------------------ helpers
    def _uid(self, ctx, user):
        return self.e.ids.ensure(ctx, user)

    def _common_fields(self, ctx, it):
        ms = it.get("milestone")
        return {
            "description": self.rewrite(ctx, it.get("body") or ""),
            "labels": ",".join(sorted(lb["name"] for lb in it.get("labels") or [])),
            "milestone_id": self.milestones.get(ctx.name, {}).get(ms["number"]) if ms else 0,
            "assignee_ids": sorted(self._uid(ctx, a) for a in it.get("assignees") or []),
        }

    def _unchanged(self, it, kind, force):
        if force:
            return False
        for ctx in self.ctxs:
            m = self.db.map_get(ctx.name, kind, str(it["id"]))
            if not m or m.get("gh_updated") != it["updated_at"]:
                return False
        return True

    def _with_reactions(self, objs):
        for o in objs:
            rx = o.get("reactions") or {}
            o["_reactions"] = self.gh.reactions(rx["url"]) if rx.get("total_count") else []
        return objs

    def rewrite(self, ctx, text):
        """Copy GitHub-hosted attachments into the GitLab project and point the text at the copies."""
        if not text or not self.e.cfg.mirror_attachments:
            return text
        for url in mapping.find_attachments(text):
            k = f"{self.key}:{url}"
            row = self.db.map_get(ctx.name, "attachment", k)
            if row:
                if row["gl_id"]:
                    text = text.replace(url, row["gl_id"])
                continue
            new = None
            try:
                local = self._download_attachment(url)
                if local:
                    with open(local, "rb") as fh:
                        name = os.path.basename(local)
                        up = ctx.gl.post(f"/projects/{self.pid(ctx)}/uploads",
                                         files={"file": (name, fh, mimetypes.guess_type(name)[0] or "application/octet-stream")})
                    new = ctx.t.url + up["full_path"]
            except Exception as e:
                log.warning("attachment %s not mirrored: %s", url, e)
            self.db.map_put(ctx.name, "attachment", k, new, parent=self.key)
            if new:
                text = text.replace(url, new)
        return text

    def _download_attachment(self, url):
        dest = self.e.archive.blob_path(self.full, "attachments", mapping.digest(url), url.rstrip("/").split("/")[-1])
        if os.path.exists(dest):
            return dest
        r = self.gh.s.get(url, headers={"Authorization": f"Bearer {self.gh.token()}"}, timeout=120)
        ctype = r.headers.get("Content-Type", "")
        if r.status_code != 200 or ctype.startswith("text/html"):
            return None
        ext = mimetypes.guess_extension(ctype.split(";")[0]) or ""
        if ext and not dest.endswith(ext):
            dest += ext
        with open(dest, "wb") as f:
            f.write(r.content)
        return dest

    # ------------------------------------------------------------------ issues
    def sync_issue(self, it, force=False):
        if self._unchanged(it, "issue", force):
            return
        n = it["number"]
        comments = self._with_reactions(self.gh.issue_comments(self.full, n)) if it.get("comments") else []
        reactions = self.gh.reactions(it["reactions"]["url"]) if it.get("reactions", {}).get("total_count") else []
        closed_by = None
        if it["state"] == "closed":
            closed_by = it.get("closed_by") or self.gh.issue(self.full, n).get("closed_by")
        self.e.archive.put(self.full, "issues", n, {"issue": it, "comments": comments, "reactions": reactions,
                                                     "closed_by": closed_by})
        errors = []
        for ctx in self.ctxs:
            try:
                self._issue_on(ctx, it, comments, reactions, closed_by)
            except Exception as e:
                log.exception("[%s] issue %s#%s", ctx.name, self.full, n)
                errors.append(f"{ctx.name}: {e}")
        if errors:
            raise ObjectFailed("; ".join(errors))

    def _issue_on(self, ctx, it, comments, reactions, closed_by):
        gl, pid, n = ctx.gl, self.pid(ctx), it["number"]
        key = str(it["id"])
        author = self._uid(ctx, it["user"])
        fields = dict(self._common_fields(ctx, it), title=it["title"], discussion_locked=bool(it.get("locked")))
        h = mapping.digest([fields, it["state"]])
        m = self.db.map_get(ctx.name, "issue", key)
        gi = None
        if not m:
            gi = gl.issue(pid, n)
            if not gi:
                payload = {k: v for k, v in fields.items() if v not in (None, 0, [])}
                payload.update(iid=n, created_at=it["created_at"])
                try:
                    gi = gl.post(f"/projects/{pid}/issues", sudo=author, json=payload,
                                 recover=lambda: gl.issue(pid, n))
                except GLError as e:
                    # "Duplicated issue": iid n exists although the lookup above missed it (an earlier
                    # attempt landed after all) -> adopt it
                    gi = gl.issue(pid, n) if e.status == 409 else None
                    if gi is None:
                        raise
                if gi["iid"] != n:
                    self.e.alert("error", f"[{ctx.name}] {self.full} issue #{n} landed on iid {gi['iid']}")
            self.db.map_put(ctx.name, "issue", key, gi["id"], gi["iid"], parent=self.key)
            m = self.db.map_get(ctx.name, "issue", key)
        iid = m["gl_iid"]
        if m["hash"] != h:
            gi = gi or gl.issue(pid, iid)
            want = "closed" if it["state"] == "closed" else "opened"
            upd = dict(fields)
            sudo = author
            if gi["state"] != want:
                upd["state_event"] = "close" if want == "closed" else "reopen"
                if want == "closed" and closed_by:
                    sudo = self._uid(ctx, closed_by)
            gl.put(f"/projects/{pid}/issues/{iid}", sudo=sudo, json=upd)
        parent = f"issue:{key}"
        self._notes_on(ctx, pid, "issues", iid, parent, comments)
        self._awards_on(ctx, f"/projects/{pid}/issues/{iid}", parent, reactions)
        if m.get("gh_updated") != it["updated_at"]:
            op = {"project_id": pid, "iid": iid, "created_at": it["created_at"], "updated_at": it["updated_at"]}
            if it["state"] == "closed":
                op.update(closed_at=it.get("closed_at"), closed_by_id=self._uid(ctx, closed_by) if closed_by else None)
            if ctx.sc.enabled:
                self.sc_add(ctx, "issue_meta", **op)
        self.db.map_put(ctx.name, "issue", key, m["gl_id"], iid, parent=self.key, hash=h, gh_updated=it["updated_at"])

    # ------------------------------------------------------------------ notes & awards (shared)
    @staticmethod
    def _adopt(candidates, uid, created_at, taken, bodies):
        """Find an existing GitLab note for a GitHub comment (DB rebuilt, or seeded by GitLab's importer).

        Author + creation second + body must all match: reviews and their line comments share author
        and second, so the body is what tells them apart.
        """
        want = mapping.iso(created_at)
        bodies = {b.strip() for b in bodies if b}
        for n in candidates:
            if n["id"] in taken or (n.get("author") or {}).get("id") != uid:
                continue
            if mapping.iso(n["created_at"]) == want and (n.get("body") or "").strip() in bodies:
                return n
        return None

    def _note_finder(self, gl, path, uid, created_at, taken, bodies):
        return lambda: self._adopt([x for x in gl.paginate(path) if not x.get("system")], uid, created_at, taken, bodies)

    def _discussion_finder(self, gl, pid, iid, uid, created_at, taken, bodies):
        def find():
            for d in gl.discussions(pid, "merge_requests", iid):
                if self._adopt(d["notes"][:1], uid, created_at, taken, bodies):
                    return d
            return None
        return find

    def _taken(self, ctx, parent):
        """GitLab note ids already mapped to any GitHub object under this issue/MR."""
        rows = self.db.rows("SELECT gl_id FROM mapping WHERE target=? AND kind IN ('note','review','rc') AND parent=?",
                            (ctx.name, parent))
        return {int(r["gl_id"]) for r in rows if r["gl_id"]}

    def _notes_on(self, ctx, pid, noun, iid, parent, comments):
        gl = ctx.gl
        existing = self.db.map_children(ctx.name, "note", parent)
        taken = self._taken(ctx, parent)
        current = None
        seen = set()
        for c in sorted(comments, key=lambda c: (c["created_at"], c["id"])):
            k = f"ic:{c['id']}"
            seen.add(k)
            uid = self._uid(ctx, c["user"])
            body = self.rewrite(ctx, c.get("body") or "") or "​"
            h = mapping.digest(body)
            row = existing.get(k)
            if row and row["hash"] == h:
                note_id = int(row["gl_id"])
            elif row:
                note_id = int(row["gl_id"])
                gl.put(f"/projects/{pid}/{noun}/{iid}/notes/{note_id}", sudo=uid, json={"body": body})
            else:
                if current is None:
                    current = [x for x in gl.notes(pid, noun, iid) if not x.get("system")]
                hit = self._adopt(current, uid, c["created_at"], taken, [body, c.get("body")])
                if hit:
                    note_id = hit["id"]
                    if hit["body"] != body:
                        gl.put(f"/projects/{pid}/{noun}/{iid}/notes/{note_id}", sudo=uid, json={"body": body})
                else:
                    note_id = gl.post(f"/projects/{pid}/{noun}/{iid}/notes", sudo=uid,
                                      json={"body": body, "created_at": c["created_at"]},
                                      recover=self._note_finder(gl, f"/projects/{pid}/{noun}/{iid}/notes", uid,
                                                                c["created_at"], taken, [body]))["id"]
                taken.add(note_id)
            self.db.map_put(ctx.name, "note", k, note_id, parent=parent, hash=h, gh_updated=c["updated_at"])
            self._awards_on(ctx, f"/projects/{pid}/{noun}/{iid}/notes/{note_id}", f"{parent}:{k}",
                            c.get("_reactions", []))
        for k, row in existing.items():
            if k.startswith("ic:") and k not in seen:
                gl.delete(f"/projects/{pid}/{noun}/{iid}/notes/{row['gl_id']}", ok404=True)
                self.db.map_delete(ctx.name, "note", k)
                self.db.audit(ctx.name, "note.delete", {"repo": self.full, "parent": parent, "gh": k})

    def _awards_on(self, ctx, prefix, parent, reactions):
        gl = ctx.gl
        existing = self.db.map_children(ctx.name, "award", parent)
        seen = set()
        for r in reactions:
            name = mapping.EMOJI.get(r.get("content"))
            if not name:
                continue
            k = f"{parent}:{r['id']}"
            seen.add(k)
            if k in existing:
                continue
            uid = self._uid(ctx, r["user"])
            try:
                aid = gl.post(f"{prefix}/award_emoji", sudo=uid, json={"name": name})["id"]
            except GLError as e:
                if e.status not in (400, 404, 409):
                    raise
                aid = next((a["id"] for a in gl.paginate(f"{prefix}/award_emoji")
                            if a["name"] == name and a["user"]["id"] == uid), None)
                if aid is None:
                    raise
            self.db.map_put(ctx.name, "award", k, aid, parent=parent)
        for k, row in existing.items():
            if k not in seen:
                gl.delete(f"{prefix}/award_emoji/{row['gl_id']}", ok404=True)
                self.db.map_delete(ctx.name, "award", k)

    # ------------------------------------------------------------------ pull requests
    def sync_pr(self, it, force=False):
        if self._unchanged(it, "mr", force):
            return
        n = it["number"]
        pr = self.gh.pull(self.full, n)
        comments = self._with_reactions(self.gh.issue_comments(self.full, n)) if it.get("comments") else []
        reviews = self.gh.reviews(self.full, n)
        rcomments = self._with_reactions(self.gh.review_comments(self.full, n)) if pr.get("review_comments") else []
        threads = self.gh.review_threads(self.owner, self.name, n) if rcomments else []
        reactions = self.gh.reactions(it["reactions"]["url"]) if it.get("reactions", {}).get("total_count") else []
        closed_by = None
        if pr["state"] == "closed" and not pr.get("merged_at"):
            closed_by = self.gh.issue(self.full, n).get("closed_by")
        pr["_issue_updated_at"] = it["updated_at"]
        self.e.archive.put(self.full, "pulls", n, {"pull": pr, "comments": comments, "reviews": reviews,
                                                    "review_comments": rcomments, "threads": threads,
                                                    "reactions": reactions, "closed_by": closed_by})
        errors = []
        for ctx in self.ctxs:
            try:
                self._pr_on(ctx, pr, it, comments, reviews, rcomments, threads, reactions, closed_by)
            except Exception as e:
                log.exception("[%s] PR %s#%s", ctx.name, self.full, n)
                errors.append(f"{ctx.name}: {e}")
        if errors:
            raise ObjectFailed("; ".join(errors))

    def _git_target(self, ctx):
        p = self.projects[ctx.name]
        return ctx.gl.git_url(p["path_with_namespace"]), ctx.gl.git_env()

    def _create_mr(self, ctx, pr, fields, author, state):
        gl, pid, n = ctx.gl, self.pid(ctx), pr["number"]
        mpath = self.mirror_path
        head_sha = self.e.mirror.ref_sha(mpath, f"refs/pull/{n}/head") or pr["head"]["sha"]
        same_repo = (pr["head"].get("repo") or {}).get("full_name") == self.full
        push = {}
        if state == "opened" and same_repo and self.e.mirror.ref_sha(mpath, f"refs/heads/{pr['head']['ref']}"):
            source, target = pr["head"]["ref"], pr["base"]["ref"]
        else:
            source = f"gh-pr/{n}"
            push[f"refs/heads/{source}"] = head_sha
            base_exists = self.e.mirror.ref_sha(mpath, f"refs/heads/{pr['base']['ref']}")
            if (state != "opened" and ctx.sc.enabled or not base_exists) and \
                    self.e.mirror.has_commit(mpath, pr["base"]["sha"]):
                # diff against the PR's own base commit; the real target branch is restored by the side channel
                target = f"gh-pr-base/{n}"
                push[f"refs/heads/{target}"] = pr["base"]["sha"]
            else:
                target = pr["base"]["ref"]
        if push:
            self._create_branches(ctx, {ref[len("refs/heads/"):]: sha for ref, sha in push.items()}, n)
        payload = {k: v for k, v in fields.items() if v not in (None, 0, [])}
        payload.update(source_branch=source, target_branch=target, remove_source_branch=False)
        burned = 0
        while True:
            mr = self._post_mr(ctx, pid, author, payload)
            if mr["iid"] >= n:
                break
            gl.delete(f"/projects/{pid}/merge_requests/{mr['iid']}")   # advance the iid counter (number parity)
            burned += 1
        if burned:
            self.db.audit(ctx.name, "mr.iid_advance", {"repo": self.full, "pr": n, "burned": burned})
        if mr["iid"] != n and not ctx.sc.enabled:
            self.e.alert("error", f"[{ctx.name}] {self.full} PR #{n} landed on MR !{mr['iid']} (number parity lost)")
        return mr, {"source": source, "temp_refs": [r for r in push if r.startswith("refs/heads/gh-pr-base/")]
                    + ([f"refs/heads/{source}"] if source.startswith("gh-pr/") else [])}

    def _fix_mr_iid(self, ctx, pid, m, iid, n):
        res = ctx.sc.run([{"op": "mr_iid", "project_id": pid, "from_iid": iid, "to_iid": n}])[0]
        if not res["ok"]:
            log.warning("[%s] %s: MR !%s cannot move to !%s: %s", ctx.name, self.full, iid, n, res.get("error"))
            return iid
        self.db.map_put(ctx.name, "mr", m["gh_id"], m["gl_id"], n, parent=m["parent"], extra=m["extra"],
                        hash=None, gh_updated=None)
        m.update(gl_iid=n, hash=None, gh_updated=None)
        self.e.alert("warning", f"[{ctx.name}] {self.full}: MR !{iid} renumbered to !{n} (number parity restored)")
        return n

    def _create_branches(self, ctx, branches, n):
        """Create helper branches at exact SHAs.

        The commits are first pushed to hidden refs (refs/ghmirror/pull/N/...), which also keeps them alive
        for the MR's diff forever; the branches are then created through the API, which updates GitLab's
        branch cache synchronously (a plain git push is only noticed after an async post-receive job).
        """
        url, env = self._git_target(ctx)
        hidden = {f"refs/ghmirror/pull/{n}/{'base' if b.startswith('gh-pr-base/') else 'head'}": sha
                  for b, sha in branches.items()}
        self.e.mirror.push_refs(self.mirror_path, url, env, [f"+{sha}:{ref}" for ref, sha in hidden.items()])
        pid = self.pid(ctx)
        for b, sha in branches.items():
            cur = ctx.gl.get(f"/projects/{pid}/repository/branches/{enc(b)}", ok404=True)
            if cur and cur["commit"]["id"] == sha:
                continue
            if cur:
                ctx.gl.delete(f"/projects/{pid}/repository/branches/{enc(b)}", ok404=True)
            ctx.gl.post(f"/projects/{pid}/repository/branches", json={"branch": b, "ref": sha},
                        recover=lambda: ctx.gl.get(f"/projects/{pid}/repository/branches/{enc(b)}", ok404=True))

    def _post_mr(self, ctx, pid, author, payload):
        for attempt in range(12):
            try:
                # GitLab allows one open MR per source/target pair, so that pair identifies a resent create
                return ctx.gl.post(f"/projects/{pid}/merge_requests", sudo=author, json=payload, recover=lambda: next(
                    iter(ctx.gl.get(f"/projects/{pid}/merge_requests", params={
                        "state": "opened", "source_branch": payload["source_branch"],
                        "target_branch": payload["target_branch"]})), None))
            except GLError as e:
                if e.status == 400 and "does not exist" in e.text and attempt < 11:
                    time.sleep(5 + 5 * attempt)   # branch cache not refreshed yet after a push
                    continue
                raise

    def _mr_version(self, ctx, pid, iid):
        for _ in range(60):
            vs = ctx.gl.get(f"/projects/{pid}/merge_requests/{iid}/versions")
            if vs and vs[0].get("state") in ("collected", "overflow", "without_files", "empty"):
                v = ctx.gl.get(f"/projects/{pid}/merge_requests/{iid}/versions/{vs[0]['id']}")
                diffs = {}
                for d in v.get("diffs") or []:
                    diffs.setdefault(d["new_path"], d)
                    diffs.setdefault(d["old_path"], d)
                return {"refs": {"base_sha": v["base_commit_sha"], "start_sha": v["start_commit_sha"],
                                 "head_sha": v["head_commit_sha"]}, "diffs": diffs}
            time.sleep(2)
        return None

    def _pr_on(self, ctx, pr, it, comments, reviews, rcomments, threads, reactions, closed_by):
        gl, pid, n = ctx.gl, self.pid(ctx), pr["number"]
        key = str(it["id"])   # issue id, same key space as the issues listing used for change detection
        author = self._uid(ctx, pr["user"])
        state = mapping.pr_state(pr)
        fields = dict(self._common_fields(ctx, pr), title=mapping.mr_title(pr),
                      reviewer_ids=sorted(self._uid(ctx, u) for u in pr.get("requested_reviewers") or []))
        h = mapping.digest([fields, state, pr["base"]["ref"], pr["head"]["sha"]])
        m = self.db.map_get(ctx.name, "mr", key)
        mr = None
        if not m:
            mr = gl.mr(pid, n)
            extra = {"source": mr["source_branch"], "temp_refs": []} if mr else None
            if not mr:
                if self.mr_create_stop is not None and n > self.mr_create_stop:
                    raise ObjectFailed(f"MR creation deferred: PR #{self.mr_create_stop} must get its number first")
                try:
                    mr, extra = self._create_mr(ctx, pr, fields, author, state)
                except Exception:
                    self.mr_create_stop = n if self.mr_create_stop is None else min(n, self.mr_create_stop)
                    raise
            self.db.map_put(ctx.name, "mr", key, mr["id"], mr["iid"], parent=self.key, extra=extra)
            m = self.db.map_get(ctx.name, "mr", key)
        iid = m["gl_iid"]
        if iid != n and ctx.sc.enabled:
            iid = self._fix_mr_iid(ctx, pid, m, iid, n)
            mr = None
        mr = mr or gl.mr(pid, iid)
        if m["hash"] != h:
            upd = dict(fields)
            sudo = author
            if state == "opened" and mr["state"] == "opened" and mr["target_branch"] != pr["base"]["ref"] \
                    and not mr["target_branch"].startswith("gh-pr-base/"):
                upd["target_branch"] = pr["base"]["ref"]
            if state == "closed" and mr["state"] == "opened":
                upd["state_event"] = "close"
                sudo = self._uid(ctx, closed_by) if closed_by else author
            if state == "opened" and mr["state"] == "closed":
                self._ensure_source_branch(ctx, mr, pr)
                upd["state_event"] = "reopen"
            if state == "merged" and not ctx.sc.enabled and mr["state"] == "opened":
                upd["state_event"] = "close"   # degraded mode: no side channel to set 'merged'
            gl.put(f"/projects/{pid}/merge_requests/{iid}", sudo=sudo, json=upd)
            mr = gl.mr(pid, iid)
        if state == "opened" and mr["source_branch"].startswith("gh-pr/"):
            self._refresh_fork_source(ctx, mr, pr)
        parent = f"mr:{key}"
        self._notes_on(ctx, pid, "merge_requests", iid, parent, comments)
        self._reviews_on(ctx, pid, iid, parent, reviews)
        self._review_comments_on(ctx, pid, iid, parent, rcomments, threads)
        self._awards_on(ctx, f"/projects/{pid}/merge_requests/{iid}", parent, reactions)
        self._mr_meta(ctx, pid, iid, mr, pr, state, closed_by, m)
        self.db.map_put(ctx.name, "mr", key, m["gl_id"], iid, parent=self.key, extra=m["extra"], hash=h,
                        gh_updated=it["updated_at"])

    def _ensure_source_branch(self, ctx, mr, pr):
        sha = self.e.mirror.ref_sha(self.mirror_path, f"refs/pull/{pr['number']}/head") or pr["head"]["sha"]
        if not ctx.gl.get(f"/projects/{self.pid(ctx)}/repository/branches/{enc(mr['source_branch'])}", ok404=True):
            self._create_branches(ctx, {mr["source_branch"]: sha}, pr["number"])

    def _refresh_fork_source(self, ctx, mr, pr):
        sha = self.e.mirror.ref_sha(self.mirror_path, f"refs/pull/{pr['number']}/head")
        if sha and sha != mr.get("sha"):
            url, env = self._git_target(ctx)
            self.e.mirror.push_refs(self.mirror_path, url, env, [f"+{sha}:refs/heads/{mr['source_branch']}"])

    def _mr_meta(self, ctx, pid, iid, mr, pr, state, closed_by, m):
        """Queue a side-channel fix when GitLab's view of dates/state/merger differs from GitHub's."""
        want_target = pr["base"]["ref"]
        merged_by = self._uid(ctx, pr["merged_by"]) if state == "merged" and pr.get("merged_by") else None
        merge_user = (mr.get("merge_user") or mr.get("merged_by") or {}).get("id")
        drift = (mapping.iso(mr["created_at"]) != mapping.iso(pr["created_at"]) or mr["state"] != state
                 or mr["target_branch"] != want_target
                 or (state == "merged" and (merge_user != merged_by or
                                            mapping.iso(mr.get("merged_at")) != mapping.iso(pr["merged_at"])))
                 or (state == "closed" and mapping.iso(mr.get("closed_at")) != mapping.iso(pr.get("closed_at"))))
        temp = m["extra"].get("temp_refs") or []
        if not drift and not (temp and state != "opened"):
            return
        if not ctx.sc.enabled:
            if state == "merged" and not m["extra"].get("merged_note"):
                who = f"@{pr['merged_by']['login']}" if pr.get("merged_by") else "someone"
                ctx.gl.post(f"/projects/{pid}/merge_requests/{iid}/notes", json={
                    "body": f"Merged on GitHub by {who} at {pr['merged_at']} as {pr.get('merge_commit_sha')}."})
                m["extra"]["merged_note"] = True
            return
        op = {"project_id": pid, "iid": iid, "created_at": pr["created_at"],
              "updated_at": pr.get("_issue_updated_at") or pr["updated_at"], "target_branch": want_target}
        if state == "merged":
            op.update(state="merged", merged_by_id=merged_by, merged_at=pr["merged_at"],
                      merge_commit_sha=pr.get("merge_commit_sha"))
        elif state == "closed":
            op.update(state="closed", closed_at=pr.get("closed_at"),
                      closed_by_id=self._uid(ctx, closed_by) if closed_by else None)
        i = self.sc_add(ctx, "mr_meta", **op)
        if temp and state != "opened":
            def cleanup(ctx=ctx, m=m, temp=temp):
                delete = self._branch_deleter(ctx)
                for r in temp:
                    delete(r[len("refs/heads/"):])
                m["extra"]["temp_refs"] = []
                self.db.map_update(ctx.name, "mr", m["gh_id"], extra=m["extra"])
            self.post_flush.setdefault(ctx.name, []).append((i, cleanup))

    def _reviews_on(self, ctx, pid, iid, parent, reviews):
        gl = ctx.gl
        existing = self.db.map_children(ctx.name, "review", parent)
        taken = self._taken(ctx, parent)
        current = None
        latest = {}
        for rv in sorted(reviews, key=lambda r: (r.get("submitted_at") or "", r["id"])):
            if rv.get("state") == "PENDING" or not rv.get("user"):
                continue
            uid = self._uid(ctx, rv["user"])
            if rv["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[uid] = (rv["state"], rv.get("submitted_at"))
            body = mapping.review_note_body(rv)
            if body is None:
                continue
            body = self.rewrite(ctx, body)
            k = f"rv:{rv['id']}"
            h = mapping.digest(body)
            row = existing.get(k)
            if row and row["hash"] == h:
                continue
            if row:
                gl.put(f"/projects/{pid}/merge_requests/{iid}/notes/{row['gl_id']}", sudo=uid, json={"body": body})
                note_id = row["gl_id"]
            else:
                if current is None:
                    current = [x for x in gl.notes(pid, "merge_requests", iid) if not x.get("system")]
                hit = self._adopt(current, uid, rv["submitted_at"], taken, [body])
                note_id = hit["id"] if hit else gl.post(
                    f"/projects/{pid}/merge_requests/{iid}/notes", sudo=uid,
                    json={"body": body, "created_at": rv["submitted_at"]},
                    recover=self._note_finder(gl, f"/projects/{pid}/merge_requests/{iid}/notes", uid,
                                              rv["submitted_at"], taken, [body]))["id"]
                taken.add(int(note_id))
            self.db.map_put(ctx.name, "review", k, note_id, parent=parent, hash=h)
        want = {uid: ts for uid, (st, ts) in latest.items() if st == "APPROVED"}
        appr = gl.get(f"/projects/{pid}/merge_requests/{iid}/approvals")
        have = {a["user"]["id"] for a in (appr or {}).get("approved_by", [])}
        for uid, ts in want.items():
            if uid not in have:
                if ctx.sc.enabled:
                    self.sc_add(ctx, "approval", project_id=pid, iid=iid, user_id=uid, created_at=ts)
                else:
                    gl.post(f"/projects/{pid}/merge_requests/{iid}/approve", sudo=uid)
        for uid in have - set(want):
            if uid == ctx.bot_id:
                continue
            if ctx.sc.enabled:
                self.sc_add(ctx, "unapprove", project_id=pid, iid=iid, user_id=uid)
            else:
                gl.post(f"/projects/{pid}/merge_requests/{iid}/unapprove", sudo=uid)

    def _review_comments_on(self, ctx, pid, iid, parent, rcomments, threads):
        gl = ctx.gl
        existing = self.db.map_children(ctx.name, "rc", parent)
        taken = self._taken(ctx, parent)
        version = None
        current = None
        seen = set()
        for c in sorted(rcomments, key=lambda c: (c["created_at"], c["id"])):
            k = f"rc:{c['id']}"
            seen.add(k)
            uid = self._uid(ctx, c["user"])
            body = self.rewrite(ctx, c.get("body") or "") or "​"
            h = mapping.digest(body)
            row = existing.get(k)
            if row:
                if row["hash"] != h:
                    shown = mapping.fallback_review_body(dict(c, body=body)) if row["extra"].get("fallback") else body
                    gl.put(f"/projects/{pid}/merge_requests/{iid}/notes/{row['gl_id']}", sudo=uid, json={"body": shown})
                    self.db.map_put(ctx.name, "rc", k, row["gl_id"], parent=parent, extra=row["extra"], hash=h)
                note_id = int(row["gl_id"])
            else:
                reply_to = existing.get(f"rc:{c.get('in_reply_to_id')}") if c.get("in_reply_to_id") else None
                if current is None:
                    discussions = gl.discussions(pid, "merge_requests", iid)
                    current = [nt for d in discussions for nt in d["notes"] if not nt.get("system")]
                    did_by_note = {nt["id"]: d["id"] for d in discussions for nt in d["notes"]}
                hit = self._adopt(current, uid, c["created_at"], taken,
                                  [body, mapping.fallback_review_body(dict(c, body=body))])
                fallback = False
                if hit:
                    note_id, did = hit["id"], did_by_note.get(hit["id"])
                elif reply_to:
                    did = reply_to["extra"]["did"]
                    note_id = gl.post(f"/projects/{pid}/merge_requests/{iid}/discussions/{did}/notes", sudo=uid,
                                      json={"body": body, "created_at": c["created_at"]},
                                      recover=lambda: self._adopt(
                                          gl.get(f"/projects/{pid}/merge_requests/{iid}/discussions/{did}")["notes"],
                                          uid, c["created_at"], taken, [body]))["id"]
                else:
                    if version is None:
                        version = self._mr_version(ctx, pid, iid) or {}
                    pos = mapping.diff_position(c, version["refs"], version["diffs"].get(c["path"])) \
                        if version.get("refs") else None
                    d = None
                    for attempt in ([pos, {kk: vv for kk, vv in pos.items() if kk != "line_range"}] if pos else []):
                        try:
                            d = gl.post(f"/projects/{pid}/merge_requests/{iid}/discussions", sudo=uid,
                                        json={"body": body, "created_at": c["created_at"], "position": attempt},
                                        recover=self._discussion_finder(gl, pid, iid, uid, c["created_at"], taken,
                                                                        [body]))
                            break
                        except GLError as e:
                            log.info("[%s] %s#%s position rejected (%s), retrying", ctx.name, self.full,
                                     iid, e.text[:120])
                    if d is None:
                        fallback = True
                        fb = mapping.fallback_review_body(dict(c, body=body))
                        d = gl.post(f"/projects/{pid}/merge_requests/{iid}/discussions", sudo=uid,
                                    json={"body": fb, "created_at": c["created_at"]},
                                    recover=self._discussion_finder(gl, pid, iid, uid, c["created_at"], taken, [fb]))
                    did, note_id = d["id"], d["notes"][0]["id"]
                taken.add(int(note_id))
                row = {"gl_id": note_id, "extra": {"did": did, "fallback": fallback}}
                existing[k] = row
                self.db.map_put(ctx.name, "rc", k, note_id, parent=parent, extra=row["extra"], hash=h)
            self._awards_on(ctx, f"/projects/{pid}/merge_requests/{iid}/notes/{note_id}", f"{parent}:{k}",
                            c.get("_reactions", []))
        for k, row in list(existing.items()):
            if k not in seen:
                gl.delete(f"/projects/{pid}/merge_requests/{iid}/notes/{row['gl_id']}", ok404=True)
                self.db.map_delete(ctx.name, "rc", k)
        for th in threads:
            row = existing.get(f"rc:{th['comment_id']}")
            if not row or row["extra"].get("resolved") == th["resolved"] or not row["extra"].get("did"):
                continue
            who = None
            if th["resolved"] and th.get("resolved_by"):
                um = self.db.row("SELECT gl_id FROM mapping WHERE target=? AND kind='user' AND extra LIKE ?",
                                 (ctx.name, f'%"login": "{th["resolved_by"]}"%'))
                who = int(um["gl_id"]) if um else None
            try:
                gl.put(f"/projects/{pid}/merge_requests/{iid}/discussions/{row['extra']['did']}", sudo=who,
                       json={"resolved": th["resolved"]})
            except GLError:
                gl.put(f"/projects/{pid}/merge_requests/{iid}/discussions/{row['extra']['did']}",
                       json={"resolved": th["resolved"]})
            row["extra"]["resolved"] = th["resolved"]
            self.db.map_update(ctx.name, "rc", f"rc:{th['comment_id']}", extra=row["extra"])

    # ------------------------------------------------------------------ releases
    def _releases_on(self, ctx, releases):
        gl, pid = ctx.gl, self.pid(ctx)
        existing = self.db.map_children(ctx.name, "release", self.key)
        seen = set()
        for rel in releases:
            if rel.get("draft"):
                self.e.archive.put(self.full, "releases", f"draft-{rel['id']}", rel)
                continue
            k = str(rel["id"])
            seen.add(k)
            tag = rel["tag_name"]
            self.e.archive.put(self.full, "releases", tag, rel)
            fields = {"name": rel.get("name") or tag, "description": self.rewrite(ctx, rel.get("body") or ""),
                      "released_at": rel.get("published_at") or rel["created_at"]}
            h = mapping.digest([fields, sorted(a["id"] for a in rel.get("assets", []))])
            row = existing.get(k)
            if row and row["hash"] == h:
                continue
            author = self._uid(ctx, rel.get("author"))
            cur = gl.get(f"/projects/{pid}/releases/{enc(tag)}", ok404=True)
            if cur:
                gl.put(f"/projects/{pid}/releases/{enc(tag)}", json=fields)
            else:
                # created as the bot: GitLab checks git access for the release author and blocked mirror
                # accounts have none; the side channel then sets the real author
                gl.post(f"/projects/{pid}/releases", json=dict(fields, tag_name=tag),
                        recover=lambda: gl.get(f"/projects/{pid}/releases/{enc(tag)}", ok404=True))
                if ctx.sc.enabled:
                    self.sc_add(ctx, "release_meta", project_id=pid, tag=tag, author_id=author, created_at=rel["created_at"])
            self._assets_on(ctx, pid, rel)
            self.db.map_put(ctx.name, "release", k, tag, parent=self.key, hash=h)
        for k, row in existing.items():
            if k not in seen:
                gl.delete(f"/projects/{pid}/releases/{enc(row['gl_id'])}", ok404=True)
                self.db.map_delete(ctx.name, "release", k)

    def _assets_on(self, ctx, pid, rel):
        gl = ctx.gl
        tag = rel["tag_name"]
        links = {ln["name"]: ln for ln in gl.get(f"/projects/{pid}/releases/{enc(tag)}/assets/links")}
        version = mapping.package_version(tag)
        for a in rel.get("assets", []):
            if a["name"] in links:
                continue
            local = self.e.archive.blob_path(self.full, "release_assets", a["id"], a["name"])
            if not os.path.exists(local) or os.path.getsize(local) != a.get("size", -1):
                self.gh.download(a["url"], local)
            fname = mapping.package_filename(a["name"])
            with open(local, "rb") as fh:
                gl.request("PUT", f"/projects/{pid}/packages/generic/github-release-assets/{version}/{enc(fname)}",
                           data=fh)
            url = f"{ctx.t.url}/api/v4/projects/{pid}/packages/generic/github-release-assets/{version}/{fname}"
            gl.post(f"/projects/{pid}/releases/{enc(tag)}/assets/links",
                    json={"name": a["name"], "url": url, "link_type": "other"},
                    recover=lambda: next((ln for ln in gl.get(f"/projects/{pid}/releases/{enc(tag)}/assets/links")
                                          if ln["name"] == a["name"]), None))
        wanted = {a["name"] for a in rel.get("assets", [])}
        for name, ln in links.items():
            if name not in wanted:
                gl.delete(f"/projects/{pid}/releases/{enc(tag)}/assets/links/{ln['id']}", ok404=True)
