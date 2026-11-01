"""Mirror ("ghost") accounts: one GitLab user per GitHub account, per target.

MIRROR mode: created active -> made Owner of the top-level mirror group -> blocked. Blocked users
cannot sign in, push or call the API, but the admin bot can act as them with the Sudo header, and
because they hold Owner, GitLab honours created_at/iid on everything written in their name.
"""
import logging
import os
import secrets
import tempfile
import threading

from . import mapping
from .gitlab import OWNER, GLError

log = logging.getLogger("ghmirror.identity")


class Identities:
    def __init__(self, cfg, db, gh, archive):
        self.cfg = cfg
        self.db = db
        self.gh = gh
        self.archive = archive
        self._profile_cache = {}
        self._lock = threading.Lock()

    def _profile(self, login):
        if login not in self._profile_cache:
            try:
                self._profile_cache[login] = self.gh.user(login)
            except Exception:  # deleted users, bots with odd logins
                self._profile_cache[login] = {}
        return self._profile_cache[login]

    def ensure(self, ctx, gh_user):
        """Return the GitLab user id mirroring gh_user on this target (creating it if needed)."""
        if not gh_user:
            return ctx.bot_id
        key = str(gh_user["id"])
        m = self.db.map_get(ctx.name, "user", key)
        if m:
            if m["extra"].get("login") != gh_user["login"]:
                self._rename(ctx, m, gh_user)
            return int(m["gl_id"])
        with self._lock:   # worker threads must not race to create the same account
            m = self.db.map_get(ctx.name, "user", key)
            if m:
                return int(m["gl_id"])
            return self._map_new(ctx, gh_user, key)

    def _map_new(self, ctx, gh_user, key):
        gl_user = self._find_or_create(ctx, gh_user)
        self.db.map_put(ctx.name, "user", key, gl_user["id"], extra={
            "login": gh_user["login"], "username": gl_user["username"], "bot": mapping.is_bot(gh_user),
            "keys_synced": False})
        self.db.audit(ctx.name, "user.map", {"login": gh_user["login"], "gl_user": gl_user["username"]})
        if self.cfg.import_keys and not mapping.is_bot(gh_user):
            self.sync_keys(ctx, gh_user["login"], gl_user["id"])
        return gl_user["id"]

    def _find_or_create(self, ctx, gh_user):
        gl = ctx.gl
        login = gh_user["login"]
        base = mapping.username_for(login)
        candidates = [base] + [f"{base}-gh{i if i > 1 else ''}" for i in range(1, 6)]
        marker = f"github_id:{gh_user['id']}"
        for cand in candidates:
            existing = gl.user_by_username(cand)
            if existing:
                full = gl.get(f"/users/{existing['id']}")
                if marker in (full.get("note") or ""):
                    self._ensure_ghost_state(ctx, full)
                    return full
                continue  # someone else owns this username -> try next candidate
            try:
                return self._create(ctx, gh_user, cand, marker)
            except GLError as e:
                text = e.text.lower()
                if "taken" in text or "namespace" in text or "already exists" in text:
                    continue  # a group or reserved path already uses this name (GitLab 19: "already exists")
                raise
        raise RuntimeError(f"no free GitLab username for {login}")

    def _create(self, ctx, gh_user, username, marker):
        gl = ctx.gl
        login = gh_user["login"]
        prof = self._profile(login) if not mapping.is_bot(gh_user) else {}
        self.archive.put_global("users", login, prof or gh_user)
        email = mapping.email_for(login, self.cfg.email_domain, self.cfg.email_map)
        data = {"username": username, "name": (prof.get("name") or login)[:255], "email": email,
                "password": secrets.token_urlsafe(24) + "aA1!", "skip_confirmation": True,
                "note": f"ghmirror {marker} login:{login}", "projects_limit": 0, "can_create_group": False,
                "bio": (prof.get("bio") or "")[:255], "organization": (prof.get("company") or "")[:255]}
        def mine():
            u = gl.user_by_username(username)
            return gl.get(f"/users/{u['id']}") if u else None
        try:
            u = gl.post("/users", data=data, recover=mine)
        except GLError as e:
            if "Email has already been taken" in e.text:
                data["email"] = mapping.email_for(f"{login}+{gh_user['id']}", self.cfg.email_domain)
                u = gl.post("/users", data=data, recover=mine)
            else:
                raise
        if self.cfg.import_avatars and gh_user.get("avatar_url"):
            self._avatar(gl, u["id"], gh_user["avatar_url"])
        self._ensure_ghost_state(ctx, u)
        log.info("[%s] created mirror account %s for %s", ctx.name, username, login)
        return u

    def _ensure_ghost_state(self, ctx, u):
        """Owner of the top-level group, then blocked (MIRROR mode invariant)."""
        if self.db.get(f"mode:{ctx.name}", "mirror") != "mirror":
            return
        gl = ctx.gl
        if not gl.get(f"/groups/{ctx.group_id}/members/{u['id']}", ok404=True):
            if u.get("state") == "blocked":
                gl.post(f"/users/{u['id']}/unblock")
            gl.post(f"/groups/{ctx.group_id}/members", data={"user_id": u["id"], "access_level": OWNER},
                    recover=lambda: gl.get(f"/groups/{ctx.group_id}/members/{u['id']}", ok404=True))
            u["state"] = "active"
        if u.get("state") != "blocked":
            gl.post(f"/users/{u['id']}/block")

    def _avatar(self, gl, uid, url):
        try:
            r = self.gh.s.get(url, timeout=30)
            if r.status_code != 200:
                return
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(r.content)
            with open(f.name, "rb") as fh:
                gl.put(f"/users/{uid}", files={"avatar": ("avatar.png", fh, "image/png")})
            os.unlink(f.name)
        except Exception as e:  # cosmetic; never fail a sync on avatars
            log.warning("avatar upload failed for %s: %s", uid, e)

    def _rename(self, ctx, m, gh_user):
        new = mapping.username_for(gh_user["login"])
        try:
            ctx.gl.put(f"/users/{m['gl_id']}", data={"username": new})
            m["extra"].update(login=gh_user["login"], username=new)
        except GLError as e:
            log.warning("[%s] rename %s -> %s failed: %s", ctx.name, m["extra"].get("username"), new, e)
            m["extra"]["login"] = gh_user["login"]
        self.db.map_put(ctx.name, "user", m["gh_id"], m["gl_id"], extra=m["extra"])

    def sync_keys(self, ctx, login, uid):
        """Copy public SSH and GPG keys so every developer's existing key works on day one."""
        gl = ctx.gl
        try:
            ssh = self.gh.user_keys(login)
            gpg = self.gh.user_gpg_keys(login)
        except Exception as e:
            log.warning("cannot read keys for %s: %s", login, e)
            return
        have = {" ".join(k["key"].split()[:2]) for k in gl.paginate(f"/users/{uid}/keys")}
        for k in ssh:
            material = " ".join(k["key"].split()[:2])
            if material not in have:
                try:
                    gl.post(f"/users/{uid}/keys", data={"title": f"github-{k['id']}", "key": k["key"]})
                except GLError as e:
                    log.warning("ssh key %s for %s rejected: %s", k["id"], login, e)
        have_gpg = {g.get("key", "").strip() for g in gl.paginate(f"/users/{uid}/gpg_keys")}
        for g in gpg:
            raw = (g.get("raw_key") or "").strip()
            if raw and raw not in have_gpg:
                try:
                    gl.post(f"/users/{uid}/gpg_keys", data={"key": raw})
                except GLError as e:
                    log.warning("gpg key %s for %s rejected: %s", g.get("key_id"), login, e)
        m = self.db.map_get(ctx.name, "user", self.db.row(
            "SELECT gh_id FROM mapping WHERE target=? AND kind='user' AND gl_id=?", (ctx.name, str(uid)))["gh_id"])
        if m:
            m["extra"]["keys_synced"] = True
            self.db.map_put(ctx.name, "user", m["gh_id"], m["gl_id"], extra=m["extra"])

    def username(self, ctx, gl_id):
        r = self.db.row("SELECT extra FROM mapping WHERE target=? AND kind='user' AND gl_id=?", (ctx.name, str(gl_id)))
        import json
        return json.loads(r["extra"]).get("username") if r else None
