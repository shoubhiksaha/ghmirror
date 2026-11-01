"""Failover: turn one GitLab target into the primary code host (and back, for drills).

promote --primary NAME:
  1. stop the GitHub source (engine writes nothing more from GitHub)
  2. replace the bot-only wildcard rule with the real branch rules archived from GitHub
  3. enable CI/CD on every mirrored project
  4. turn mirror accounts into real ones: unblock humans (bots and ex-members stay blocked),
     apply company emails, drop the temporary Owner role, apply real memberships
  5. broadcast a banner
  6. every other target becomes a secondary fed by GitLab push mirrors from the primary
demote reverses all of it using the record written by promote.
"""
import logging
import time
from urllib.parse import urlsplit

from .gitlab import DEVELOPER, MAINTAINER, OWNER, GLError, enc

log = logging.getLogger("ghmirror.promote")


def _projects(eng, ctx):
    return [r for r in eng.db.map_all(ctx.name, "repo")]


def _protection_rules(protection):
    """GitHub classic branch protection -> GitLab protected-branch settings."""
    if not protection or protection.get("detail") == "unreadable":
        return {"push_access_level": MAINTAINER, "merge_access_level": DEVELOPER, "allow_force_push": False}
    reviews = protection.get("required_pull_request_reviews")
    restrict = protection.get("restrictions")
    push = 0 if reviews else (MAINTAINER if restrict else DEVELOPER)
    return {"push_access_level": push, "merge_access_level": MAINTAINER if restrict else DEVELOPER,
            "allow_force_push": bool((protection.get("allow_force_pushes") or {}).get("enabled")),
            "code_owner_approval_required": bool(reviews and reviews.get("require_code_owner_reviews"))}


def promote(eng, primary, yes=False):
    """Every change is recorded in kv `promotion` as it is made, so a crash or a failed step part-way
    through can always be undone by demote. One failing item (a project, an account, a mirror) is
    recorded in record["failures"] and alerted; it never stops the rest of the failover."""
    eng.prepare()
    db = eng.db
    p = eng.ctx(primary)
    prev = db.get("promotion")
    if prev:
        raise RuntimeError(f"already promoted ({prev.get('state', 'done')}, primary {prev['primary']}); "
                           f"run `ghmirror demote` first")
    record = {"primary": primary, "ts": time.time(), "state": "in_progress", "projects": {}, "users": [],
              "members": [], "mirrors": [], "allowlist": [], "broadcast": None, "rpo": {}, "failures": []}

    def save():
        db.set("promotion", record)

    def failed(what, e):
        msg = getattr(e, "text", None) or str(e)
        record["failures"].append({"step": what, "error": msg[:300]})
        log.error("[%s] promote: %s failed: %s", primary, what, msg[:300])
        save()

    save()
    db.set("source", "stopped")
    log.info("source adapter stopped")
    for row in _projects(eng, p):
        pid = int(row["gl_id"])
        full = row["extra"].get("full_name")
        last = db.get(f"lastmeta:{primary}:{row['gh_id']}")
        record["rpo"][full] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last)) if last else None
        record["projects"][str(pid)] = {"full": full, "protections": []}
        save()
        try:
            _promote_project(eng, p, pid, full, record["projects"][str(pid)]["protections"])
        except Exception as e:
            failed(f"project {full}", e)
    save()
    desired = db.get(f"desired_members:{primary}") or {"group": {}, "subgroups": {}, "shares": []}
    for u in db.map_all(primary, "user"):
        uid = int(u["gl_id"])
        x = u["extra"]
        human = not x.get("bot") and x.get("login") not in ("ghost",)
        try:
            if human:
                email = eng.cfg.email_map.get(x.get("login"))
                if email:
                    try:
                        p.gl.post(f"/users/{uid}/emails", json={"email": email, "skip_confirmation": True})
                    except GLError:
                        pass   # already added
                    p.gl.put(f"/users/{uid}", json={"email": email, "skip_reconfirmation": True})
                p.gl.post(f"/users/{uid}/unblock")
                record["users"].append(uid)
            if str(uid) not in desired["group"]:
                p.gl.delete(f"/groups/{p.group_id}/members/{uid}", ok404=True)
        except Exception as e:
            failed(f"account {x.get('login')}", e)
        save()
    for uid, level in desired["group"].items():
        _set_member(p, f"/groups/{p.group_id}", int(uid), level)
        record["members"].append(["group", p.group_id, int(uid)])
    for gid, members in desired.get("subgroups", {}).items():
        for uid, level in members.items():
            _set_member(p, f"/groups/{gid}", int(uid), level)
            record["members"].append(["group", int(gid), int(uid)])
    for row in _projects(eng, p):
        pid = int(row["gl_id"])
        for uid, level in (db.get(f"desired_project_members:{primary}:{pid}") or {}).items():
            if str(uid) in desired["group"] and desired["group"][str(uid)] >= level:
                continue
            _set_member(p, f"/projects/{pid}", int(uid), level)
            record["members"].append(["project", pid, int(uid)])
    save()
    try:
        bm = p.gl.post("/broadcast_messages", json={"message": eng.cfg.broadcast_message, "broadcast_type": "banner"})
        record["broadcast"] = bm["id"]
    except GLError as e:
        failed("broadcast banner", e)
    save()
    for s in eng.ctxs:
        if s.name == primary:
            continue
        if not s.group_id:   # unreachable at promote time: say so, the team must not assume a second copy
            failed(f"secondary {s.name}", RuntimeError(
                "unreachable, no push mirrors configured; once it is back, add them in GitLab "
                "(Settings > Repository > Mirroring repositories) or run demote + promote in a drill window"))
            continue
        db.set(f"mode:{s.name}", "secondary")
        base = s.t.mirror_url or s.t.git_url or s.t.url
        scheme, rest = base.split("://", 1)
        try:
            if _allow_outbound(p, urlsplit(base).hostname, record):
                time.sleep(65)   # GitLab caches application settings for 60 s; a mirror sync before then is refused
        except Exception as e:
            failed(f"outbound allowlist for {s.name}", e)
        save()
        for row in _projects(eng, p):
            sm = db.map_get(s.name, "repo", row["gh_id"])
            if not sm:
                continue
            spath = sm["extra"]["path"]
            url = f"{scheme}://ghmirror:{s.t.token}@{rest.rstrip('/')}/{spath}.git"
            try:
                m = p.gl.post(f"/projects/{row['gl_id']}/remote_mirrors", json={
                    "url": url, "enabled": True, "only_protected_branches": False, "keep_divergent_refs": False})
            except Exception as e:
                failed(f"push mirror {row['extra'].get('full_name')} -> {s.name}", e)
                continue
            record["mirrors"].append([int(row["gl_id"]), m["id"]])
            save()
            try:
                p.gl.post(f"/projects/{row['gl_id']}/remote_mirrors/{m['id']}/sync")
            except GLError:
                pass   # first sync also runs on the next push
    db.set(f"mode:{primary}", "primary")
    record["state"] = "partial" if record["failures"] else "done"
    save()
    eng.alert("critical", f"PROMOTED {primary} to primary; {len(record['users'])} accounts activated, "
                          f"{len(record['mirrors'])} push mirrors to secondaries"
                          + (f"; {len(record['failures'])} FAILED steps: {record['failures'][:3]} "
                             f"(fix, then `ghmirror demote` + `ghmirror promote` again)" if record["failures"] else ""))
    return record


def _promote_project(eng, p, pid, full, applied):
    """Replace the bot-only rule with the archived GitHub branch protection and enable CI."""
    prot = eng.archive.get(full, "settings", "branch_protection") or {}
    for r in p.gl.get(f"/projects/{pid}/protected_branches"):
        p.gl.delete(f"/projects/{pid}/protected_branches/{enc(r['name'])}", ok404=True)
    for branch, rule in prot.items():
        data = dict(_protection_rules(rule), name=branch)
        try:
            p.gl.post(f"/projects/{pid}/protected_branches", json=data)
        except GLError as e:   # e.g. code-owner approval is a Premium feature on CE
            data.pop("code_owner_approval_required", None)
            p.gl.post(f"/projects/{pid}/protected_branches", json=data)
            log.info("[%s] %s: %s (applied without code-owner approval)", p.name, full, e.text[:100])
        applied.append(branch)
    p.gl.put(f"/projects/{pid}", json={"builds_access_level": "enabled"})


def _allow_outbound(p, host, record):
    """GitLab refuses push mirrors to private-network hosts unless they are on the outbound allowlist;
    a secondary is usually exactly that (an offline box on the LAN). Add only this host, and record
    it so demote can take it out again."""
    st = p.gl.get("/application/settings")
    if st.get("allow_local_requests_from_web_hooks_and_services"):
        return False
    cur = list(st.get("outbound_local_requests_whitelist") or [])
    if host in cur:
        return False
    p.gl.put("/application/settings", json={"outbound_local_requests_allowlist_raw": "\n".join(cur + [host])})
    record["allowlist"].append(host)
    return True


def _set_member(ctx, base, uid, level):
    cur = ctx.gl.get(f"{base}/members/{uid}", ok404=True)
    try:
        if cur:
            if cur["access_level"] != level:
                ctx.gl.put(f"{base}/members/{uid}", json={"access_level": level})
        else:
            ctx.gl.post(f"{base}/members", json={"user_id": uid, "access_level": level})
    except GLError as e:
        log.warning("[%s] membership %s %s -> %s failed: %s", ctx.name, base, uid, level, e.text[:150])


def demote(eng):
    """Reverse a promotion (drills): back to MIRROR mode with the GitHub source active."""
    eng.prepare()
    db = eng.db
    rec = db.get("promotion")
    if not rec:
        raise RuntimeError("not promoted")
    p = eng.ctx(rec["primary"])
    for pid, mid in rec["mirrors"]:
        p.gl.delete(f"/projects/{pid}/remote_mirrors/{mid}", ok404=True)
    for kind, oid, uid in rec["members"]:
        base = f"/groups/{oid}" if kind == "group" else f"/projects/{oid}"
        if kind == "group" and oid == p.group_id:
            continue
        p.gl.delete(f"{base}/members/{uid}", ok404=True)
    for u in db.map_all(p.name, "user"):
        uid = int(u["gl_id"])
        _set_member(p, f"/groups/{p.group_id}", uid, OWNER)
        user = p.gl.get(f"/users/{uid}")
        if user["state"] != "blocked":
            p.gl.post(f"/users/{uid}/block")
    for pid in rec["projects"]:
        for r in p.gl.get(f"/projects/{pid}/protected_branches"):
            p.gl.delete(f"/projects/{pid}/protected_branches/{enc(r['name'])}", ok404=True)
        p.gl.post(f"/projects/{pid}/protected_branches", json={
            "name": "*", "push_access_level": MAINTAINER, "merge_access_level": MAINTAINER, "allow_force_push": True})
        p.gl.put(f"/projects/{pid}", json={"builds_access_level": "disabled"})
    if rec.get("broadcast"):
        p.gl.delete(f"/broadcast_messages/{rec['broadcast']}", ok404=True)
    if rec.get("allowlist"):
        cur = list(p.gl.get("/application/settings").get("outbound_local_requests_whitelist") or [])
        p.gl.put("/application/settings", json={
            "outbound_local_requests_allowlist_raw": "\n".join(h for h in cur if h not in rec["allowlist"])})
    for c in eng.ctxs:
        db.set(f"mode:{c.name}", "mirror")
    db.set("source", "github")
    db.delete("promotion")
    db.set("last_drill", {"promoted": rec["ts"], "demoted": time.time(), "primary": rec["primary"]})
    eng.alert("warning", f"DEMOTED {rec['primary']}: back to MIRROR mode, GitHub source active")
