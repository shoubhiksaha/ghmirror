"""Engine wiring: targets, guard (deletion-propagation circuit breaker), repository passes, alerts."""
import fnmatch
import logging
import time

import requests

from . import mapping
from .archive import Archive
from .db import DB
from .github import GitHub, NotFound, SourceUnavailable
from .gitlab import OWNER, GitLab
from .gitmirror import Mirror
from .identity import Identities
from .sidechannel import SideChannel
from .sync import RepoSync

log = logging.getLogger("ghmirror")


class Halted(Exception):
    pass


class TargetCtx:
    def __init__(self, eng, target):
        self.t = target
        self.name = target.name
        self.gl = GitLab(target)
        self.sc = SideChannel(target.side_channel, eng.db, target.name)
        self.e = eng
        self.group_id = None
        self.bot_id = None
        self.bot_username = None

    @property
    def mode(self):
        return self.e.db.get(f"mode:{self.name}", "mirror")

    def prepare(self):
        me = self.gl.get("/user")
        if not me.get("is_admin"):
            raise RuntimeError(f"[{self.name}] the target token must belong to an administrator")
        self.bot_id, self.bot_username = me["id"], me["username"]
        g = self.gl.group(self.t.group)
        if g is None:
            if "/" in self.t.group:
                raise RuntimeError(f"[{self.name}] parent of group {self.t.group} must exist")
            g = self.gl.post("/groups", json={"name": self.t.group, "path": self.t.group, "visibility": "private",
                                              "description": "GitHub mirror (managed by ghmirror)"},
                              recover=lambda: self.gl.group(self.t.group))
            log.info("[%s] created group %s", self.name, self.t.group)
        self.group_id = g["id"]
        if not self.gl.get(f"/groups/{g['id']}/members/{self.bot_id}", ok404=True):
            self.gl.post(f"/groups/{g['id']}/members", data={"user_id": self.bot_id, "access_level": OWNER},
                         recover=lambda: self.gl.get(f"/groups/{g['id']}/members/{self.bot_id}", ok404=True))


class Guard:
    """Never let a vanishing source wipe the mirrors."""

    def __init__(self, eng):
        self.e = eng
        self.db = eng.db

    def halted(self):
        return self.db.get("halted")

    def halt(self, reason):
        if not self.halted():
            self.db.set("halted", {"reason": reason, "ts": time.time()})
            self.e.alert("critical", f"GUARD HALT: {reason}. All writes stopped; mirrors keep last-known-good "
                                     f"state. Investigate, then run `ghmirror guard reset` or `ghmirror promote`.")
        raise Halted(reason)

    def reset(self):
        self.db.delete("halted")

    def check_source(self):
        try:
            self.e.gh.owner()
        except NotFound:
            self.halt(f"GitHub owner '{self.e.cfg.owner}' returns 404 (deleted, renamed or suspended?)")

    def _threshold(self, total):
        return max(int(total * self.e.cfg.max_disappear_ratio), self.e.cfg.max_disappear_count)

    def check_repos(self, repos):
        tracked = {}
        for r in self.db.rows("SELECT gh_id, extra FROM mapping WHERE kind='repo'"):
            import json
            x = json.loads(r["extra"] or "{}")
            if not x.get("deleted_on_source"):
                tracked[r["gh_id"]] = x.get("full_name")
        current = {str(r["id"]) for r in repos}
        missing = {k: v for k, v in tracked.items() if k not in current}
        if len(missing) > self._threshold(len(tracked)):
            self.halt(f"{len(missing)} of {len(tracked)} tracked repositories disappeared in one pass "
                      f"(limit {self._threshold(len(tracked))}): {sorted(missing.values())[:10]}")
        return missing

    def check_objects(self, rs, items):
        """Full listing of a repo's issues+PRs: mapped GitHub ids that vanished were deleted on GitHub."""
        present = {str(it["id"]) for it in items}
        for ctx in rs.ctxs:
            mapped = {}
            for kind in ("issue", "mr"):
                for row in self.db.rows("SELECT gh_id, gl_iid FROM mapping WHERE target=? AND kind=? AND parent=? "
                                        "AND (hash IS NULL OR hash <> 'deleted')", (ctx.name, kind, rs.key)):
                    mapped[row["gh_id"]] = (kind, row["gl_iid"])
            gone = {gh_id: v for gh_id, v in mapped.items() if gh_id not in present}
            if len(gone) > self._threshold(len(mapped)):
                self.halt(f"{rs.full}: {len(gone)} of {len(mapped)} issues/PRs disappeared in one pass")
            for gh_id, (kind, iid) in gone.items():
                self._mark_deleted(ctx, rs, kind, iid, gh_id)

    def _mark_deleted(self, ctx, rs, kind, iid, gh_id):
        noun = "issues" if kind == "issue" else "merge_requests"
        pid = rs.pid(ctx)
        try:
            labels = ctx.gl.get(f"/projects/{pid}/{noun}/{iid}")["labels"]
            ctx.gl.put(f"/projects/{pid}/{noun}/{iid}", json={
                "labels": ",".join(sorted(set(labels) | {mapping.DELETED_LABEL})), "state_event": "close"})
        except Exception as e:
            log.warning("[%s] marking %s %s#%s deleted failed: %s", ctx.name, kind, rs.full, iid, e)
            return
        self.db.map_update(ctx.name, kind, gh_id, hash="deleted")
        self.e.alert("warning", f"[{ctx.name}] {rs.full} {kind} #{iid} no longer exists on GitHub; "
                                f"kept in GitLab, closed and labelled {mapping.DELETED_LABEL}")


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.db = DB(cfg.database)
        self.db.init()
        self.gh = GitHub(cfg)
        self.archive = Archive(cfg.archive_dir)
        self.mirror = Mirror(cfg.mirrors_dir, self.gh)
        self.ids = Identities(cfg, self.db, self.gh, self.archive)
        self.guard = Guard(self)
        self.ctxs = [TargetCtx(self, t) for t in cfg.targets]
        self._prepared = False
        self._down = {}

    # ------------------------------------------------------------------ targets
    def prepare(self):
        if self._prepared:
            return
        for ctx in self.ctxs:
            try:
                ctx.prepare()
                self._down.pop(ctx.name, None)
            except Exception as e:
                log.exception("target %s unavailable", ctx.name)
                self.target_failed(ctx, e)
        self._prepared = True

    def ctx(self, name):
        return next(c for c in self.ctxs if c.name == name)

    def source_active(self):
        return self.db.get("source", "github") == "github"

    def writable_targets(self):
        """Targets the GitHub source writes to: prepared, reachable and in MIRROR mode."""
        if not self.source_active():
            return []
        return [c for c in self.ctxs if c.group_id and c.name not in self._down and c.mode == "mirror"]

    def target_failed(self, ctx, err):
        self._down[ctx.name] = str(err)
        self.db.set(f"target_error:{ctx.name}", {"error": str(err)[:500], "ts": time.time()})

    # ------------------------------------------------------------------ alerts
    def alert(self, level, message):
        log.log(logging.CRITICAL if level == "critical" else logging.ERROR if level == "error" else logging.WARNING,
                "ALERT %s: %s", level, message)
        self.db.add_alert(level, message)
        if self.cfg.alert_webhook:
            try:
                requests.post(self.cfg.alert_webhook, json={"text": f"[ghmirror {level}] {message}"}, timeout=10)
            except requests.RequestException as e:
                log.warning("alert webhook failed: %s", e)

    # ------------------------------------------------------------------ passes
    def selected(self, repo):
        name = repo["name"]
        return any(fnmatch.fnmatch(name, p) for p in self.cfg.include) and \
            not any(fnmatch.fnmatch(name, p) for p in self.cfg.exclude)

    def list_repos(self):
        """Repository listing with the deletion guard applied. Vanished repos are archived, never deleted."""
        if self.guard.halted():
            raise Halted(self.guard.halted()["reason"])
        self.prepare()
        self.guard.check_source()
        repos = [r for r in self.gh.repos() if self.selected(r)]
        missing = self.guard.check_repos(repos)
        for gh_id, full in missing.items():
            self._repo_gone(gh_id, full)
        return repos

    def _repo_gone(self, gh_id, full):
        try:
            self.gh.repo(full)
            return  # still exists (listing glitch or access scope change): leave it alone
        except NotFound:
            pass
        except SourceUnavailable:
            return
        for ctx in self.writable_targets():
            m = self.db.map_get(ctx.name, "repo", gh_id)
            if not m:
                continue
            try:
                ctx.gl.post(f"/projects/{m['gl_id']}/archive")
            except Exception as e:
                log.warning("[%s] archiving %s failed: %s", ctx.name, full, e)
                continue
            m["extra"]["deleted_on_source"] = True
            self.db.map_put(ctx.name, "repo", gh_id, m["gl_id"], extra=m["extra"], hash=m["hash"])
        self.alert("warning", f"repository {full} no longer exists on GitHub; GitLab copies archived (not deleted), "
                              f"bare mirror and raw archive kept")

    def sync_repo(self, repo, git=True, meta=True, full=False):
        if self.guard.halted():
            raise Halted(self.guard.halted()["reason"])
        self.prepare()
        rs = RepoSync(self, repo)
        if not rs.ctxs:
            return rs
        if meta:
            for ctx in rs.ctxs:
                ctx.sc.warm()
        rs.ensure_projects()
        if git or meta:   # metadata needs the PR commits and tags on GitLab first; cheap when nothing changed
            rs.sync_git()
        if meta:
            rs.sync_meta(full=full)
        return rs

    def repo_by_name(self, full):
        return self.gh.repo(full)
