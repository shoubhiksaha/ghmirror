"""Organization structure: members, teams -> subgroups, team repo access -> project shares.

In MIRROR mode every mirror account is a blocked Owner of the top group (needed for backdating), so
real memberships are only recorded here and applied by `promote`. Team subgroups and project shares
are created right away because they do not depend on the users' state.
"""
import logging

from . import mapping
from .gitlab import MAINTAINER, OWNER, GLError

log = logging.getLogger("ghmirror.org")


def sync_org(eng):
    eng.prepare()
    gh, db, cfg = eng.gh, eng.db, eng.cfg
    for ctx in eng.writable_targets():
        desired = {"group": {}, "subgroups": {}, "shares": []}
        if cfg.owner_type != "org":
            owner = gh.owner()
            desired["group"][str(eng.ids.ensure(ctx, owner))] = OWNER
            db.set(f"desired_members:{ctx.name}", desired)
            continue
        org = gh.owner()
        eng.archive.put_global("org", cfg.owner, org)
        base = mapping.ROLE.get(org.get("default_repository_permission") or "read")
        members = gh.org_members()
        eng.archive.put_global("org", "members", {k: v["role"] for k, v in members.items()})
        for login, info in members.items():
            uid = eng.ids.ensure(ctx, info["user"])
            level = OWNER if info["role"] == "admin" else base
            if level:
                desired["group"][str(uid)] = level
        for oc in gh.outside_collaborators():
            eng.ids.ensure(ctx, oc)
        teams = gh.teams()
        eng.archive.put_global("org", "teams", teams)
        if teams:
            parent = _ensure_subgroup(ctx, ctx.t.group, "teams", "GitHub teams")
        for t in teams:
            sg = _ensure_subgroup(ctx, f"{ctx.t.group}/teams", t["slug"], t.get("description") or t["name"],
                                  parent_id=parent["id"])
            members = gh.team_members(t["slug"])
            desired["subgroups"][str(sg["id"])] = {
                str(eng.ids.ensure(ctx, m["user"])): OWNER if m["role"] == "maintainer" else MAINTAINER
                for m in members.values()}
            for repo in gh.team_repos(t["slug"]):
                pm = db.map_get(ctx.name, "repo", str(repo["id"]))
                if not pm:
                    continue
                perm = repo.get("role_name") or next((p for p in ("admin", "maintain", "push", "triage", "pull")
                                                      if (repo.get("permissions") or {}).get(p)), "pull")
                level = min(mapping.ROLE.get(perm) or MAINTAINER, MAINTAINER)
                desired["shares"].append({"project_id": int(pm["gl_id"]), "group_id": sg["id"], "level": level})
                _share(ctx, int(pm["gl_id"]), sg["id"], level)
        db.set(f"desired_members:{ctx.name}", desired)


def _ensure_subgroup(ctx, parent_path, path, description, parent_id=None):
    g = ctx.gl.group(f"{parent_path}/{path}")
    if g:
        return g
    if parent_id is None:
        parent_id = ctx.gl.group(parent_path)["id"]
    return ctx.gl.post("/groups", json={"name": path, "path": path, "parent_id": parent_id,
                                        "visibility": "private", "description": description[:500]},
                       recover=lambda: ctx.gl.group(f"{parent_path}/{path}"))


def _share(ctx, project_id, group_id, level):
    shared = {s["group_id"]: s for s in ctx.gl.get(f"/projects/{project_id}").get("shared_with_groups", [])}
    if group_id in shared and shared[group_id]["group_access_level"] == level:
        return
    if group_id in shared:
        ctx.gl.delete(f"/projects/{project_id}/share/{group_id}", ok404=True)
    try:
        ctx.gl.post(f"/projects/{project_id}/share", json={"group_id": group_id, "group_access": level})
    except GLError as e:
        log.warning("[%s] share project %s with group %s failed: %s", ctx.name, project_id, group_id, e)
