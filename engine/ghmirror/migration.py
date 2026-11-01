"""Weekly GitHub migration archive: GitHub's own export of every repo + metadata (the 'nuclear' L2 copy).

Org owners: POST /orgs/{org}/migrations (classic PAT with repo + admin:org).
User accounts: POST /user/migrations. Archives are deleted by GitHub after 7 days, so download at once.
"""
import logging
import os
import time

import requests

from .github import SourceUnavailable

log = logging.getLogger("ghmirror.migration")


def run_migration(eng, wait=True, timeout=3 * 3600):
    if not (eng.cfg.migration_token or eng.cfg.token):
        log.info("migration archive skipped: set source.auth.migration_token (classic PAT, repo + admin:org) "
                 "to enable it; GitHub Apps cannot use the migrations API")
        return {"state": "skipped"}
    repos = [r["full_name"] for r in eng.gh.repos() if eng.selected(r)]
    if not repos:
        return None
    try:
        mig = eng.gh.start_migration(repos)
    except (RuntimeError, SourceUnavailable) as e:
        if " 403 " in str(e) or " 401 " in str(e):
            # OAuth/App tokens cannot use the migrations API: a configuration problem, not a transient one,
            # so alert once rather than every week
            msg = ("GitHub migration archive refused: the migrations API needs a classic PAT of an organization "
                   f"owner (repo + admin:org) in source.auth.migration_token. {str(e)[:200]}")
            if not eng.db.get("migration:refused_alerted"):
                eng.alert("error", msg)
                eng.db.set("migration:refused_alerted", time.time())
            else:
                log.warning(msg)
            return {"state": "unauthorized"}
        raise
    mid = mig["id"]
    eng.db.set("migration:last_started", {"id": mid, "ts": time.time(), "repos": len(repos)})
    log.info("migration %s started for %d repositories", mid, len(repos))
    if not wait:
        return {"id": mid, "state": mig.get("state")}
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = eng.gh.migration(mid)["state"]
        if state == "exported":
            break
        if state == "failed":
            eng.alert("error", f"GitHub migration archive {mid} failed")
            return {"id": mid, "state": state}
        time.sleep(20)
    else:
        eng.alert("error", f"GitHub migration archive {mid} not ready after {timeout}s")
        return {"id": mid, "state": "timeout"}
    url = eng.gh.migration_archive_url(mid)
    dest = os.path.join(eng.archive.migrations_dir(), f"{time.strftime('%Y%m%dT%H%M%S')}-migration-{mid}.tar.gz")
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(dest + ".part", "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    os.replace(dest + ".part", dest)
    size = os.path.getsize(dest)
    eng.db.set("migration:last_done", {"id": mid, "ts": time.time(), "path": dest, "bytes": size})
    eng.db.set("migration:refused_alerted", None)
    log.info("migration archive saved: %s (%d bytes)", dest, size)
    return {"id": mid, "state": "exported", "path": dest, "bytes": size}
