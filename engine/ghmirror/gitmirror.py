"""L1: bare mirror per repository and ref-exact pushes to each GitLab target.

refs/pull/* stay in the bare mirror only. Branch deletions and force-pushes are mirrored, but the
previous tip of every deleted or rewritten ref is first saved under refs/ghmirror-backup/ so the
bare mirror never loses history.
"""
import logging
import os
import shutil
import subprocess
import time

log = logging.getLogger("ghmirror.git")
MANAGED_PREFIXES = ("refs/heads/gh-pr/", "refs/heads/gh-pr-base/")


class GitError(Exception):
    pass


def run(args, cwd=None, env=None, check=True, timeout=7200):
    full_env = dict(os.environ)
    full_env.update(env or {})
    p = subprocess.run(["git", *args], cwd=cwd, env=full_env, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed: {p.stderr.strip()[-1500:]}")
    return p


def has_lfs():
    return shutil.which("git-lfs") is not None


class Mirror:
    def __init__(self, root, gh):
        self.root = os.path.abspath(root)
        self.gh = gh

    def path(self, full, wiki=False):
        return os.path.join(self.root, *full.split("/")[:-1], full.split("/")[-1] + (".wiki" if wiki else "") + ".git")

    def refs(self, path, patterns=("refs/heads", "refs/tags")):
        p = run(["for-each-ref", "--format=%(objectname) %(refname)", *patterns], cwd=path)
        out = {}
        for line in p.stdout.splitlines():
            sha, ref = line.split(" ", 1)
            out[ref] = sha
        return out

    def fetch(self, full, wiki=False):
        """Clone or update the bare mirror. Returns path, or None if the (wiki) repo does not exist."""
        path = self.path(full, wiki)
        env = self.gh.git_env()
        url = self.gh.clone_url(full, wiki)
        if not os.path.isdir(path):
            if wiki:
                probe = run(["ls-remote", url], env=env, check=False, timeout=300)
                if probe.returncode != 0:
                    return None
            os.makedirs(os.path.dirname(path), exist_ok=True)
            run(["clone", "--mirror", "--quiet", url, path], env=env)
            for k, v in (("core.logAllRefUpdates", "always"), ("gc.reflogExpire", "never"),
                         ("gc.reflogExpireUnreachable", "never"), ("gc.pruneExpire", "never")):
                run(["config", k, v], cwd=path)
            # explicit refspecs so `fetch --prune` never touches refs/ghmirror-backup/*
            run(["config", "--unset-all", "remote.origin.fetch"], cwd=path, check=False)
            for spec in ("+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*", "+refs/pull/*:refs/pull/*"):
                run(["config", "--add", "remote.origin.fetch", spec], cwd=path)
        else:
            before = self.refs(path, ("refs/heads", "refs/tags"))
            run(["fetch", "--prune", "--quiet", "origin"], cwd=path, env=env)
            after = self.refs(path, ("refs/heads", "refs/tags"))
            self._backup_rewritten(path, before, after)
        if not wiki and has_lfs() and self._uses_lfs(path):
            run(["lfs", "fetch", "--all", "origin"], cwd=path, env=env, check=False)
        return path

    def _uses_lfs(self, path):
        p = run(["show", "HEAD:.gitattributes"], cwd=path, check=False)
        return p.returncode == 0 and "filter=lfs" in p.stdout

    def _backup_rewritten(self, path, before, after):
        stamp = time.strftime("%Y%m%dT%H%M%S")
        for ref, old in before.items():
            new = after.get(ref)
            if new == old:
                continue
            if new is not None:
                anc = run(["merge-base", "--is-ancestor", old, new], cwd=path, check=False)
                if anc.returncode == 0:
                    continue  # fast-forward
            run(["update-ref", f"refs/ghmirror-backup/{stamp}/{ref[len('refs/'):]}", old], cwd=path, check=False)
            log.info("%s: %s %s, old tip %s kept", path, ref, "deleted" if new is None else "rewritten", old[:10])

    def remote_refs(self, url, env):
        p = run(["ls-remote", "--heads", "--tags", url], env=env, timeout=600)
        out = {}
        for line in p.stdout.splitlines():
            sha, ref = line.split("\t")
            if not ref.endswith("^{}"):
                out[ref] = sha
        return out

    def has_commit(self, path, sha):
        return run(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=path, check=False).returncode == 0

    def push(self, path, url, env, extra=None, default_branch=None, prune=True, delete_branch=None):
        """Make the target's heads/tags equal to the mirror's, plus `extra` {ref: sha} managed refs.

        Refs under MANAGED_PREFIXES on the target are never pruned here (the PR sync owns them).
        Returns {'updated': n, 'deleted': n, 'errors': [...]}.
        """
        local = self.refs(path)
        desired = dict(local)
        desired.update(extra or {})
        remote = self.remote_refs(url, env)
        updates, deletes = [], []
        for ref, sha in desired.items():
            if remote.get(ref) != sha:
                src = ref if ref in local and not (extra and ref in extra) else sha
                updates.append(f"+{src}:{ref}")
        if prune:
            for ref in remote:
                if ref not in desired and not ref.startswith(MANAGED_PREFIXES):
                    if default_branch and ref == f"refs/heads/{default_branch}":
                        continue
                    deletes.append(f":{ref}")
        # a ref about to be deleted or overwritten on the target whose commit the mirror does not have was
        # made on the target (by hand, or during a drill): save it into the mirror first, never lose it
        doomed = [d[1:] for d in deletes] + [u.split(":", 1)[1] for u in updates]
        lost = {ref: remote[ref] for ref in doomed if ref in remote and not self.has_commit(path, remote[ref])}
        if lost:
            unsaved = self._rescue(path, url, env, lost)
            if unsaved:
                errors_pre = [f"kept {ref} on the target: could not save its commits first" for ref in unsaved]
                deletes = [d for d in deletes if d[1:] not in unsaved]
                updates = [u for u in updates if u.split(":", 1)[1] not in unsaved]
            else:
                errors_pre = []
        else:
            errors_pre = []
        if default_branch:  # push the default branch first so an empty project adopts it
            first = [u for u in updates if u.endswith(f":refs/heads/{default_branch}")]
            updates = first + [u for u in updates if u not in first]
        errors = list(errors_pre)
        if delete_branch:   # protected branches cannot be deleted by git push; the caller uses the API
            for spec in [d for d in deletes if d.startswith(":refs/heads/")]:
                try:
                    delete_branch(spec[len(":refs/heads/"):])
                except Exception as e:
                    errors.append(f"delete {spec}: {e}")
            deletes = [d for d in deletes if not d.startswith(":refs/heads/")]
        for chunk in [updates[i:i + 200] for i in range(0, len(updates), 200)] + \
                     [deletes[i:i + 200] for i in range(0, len(deletes), 200)]:
            if not chunk:
                continue
            p = run(["push", "--porcelain", "--no-verify", url, *chunk], cwd=path, env=env, check=False)
            if p.returncode != 0:
                rejected = [ln for ln in p.stdout.splitlines() if ln.startswith("!")]
                errors.extend(rejected or [p.stderr.strip()[-500:]])
        if has_lfs() and updates and self._uses_lfs(path):
            run(["lfs", "push", "--all", url], cwd=path, env=env, check=False)
        return {"updated": len(updates), "deleted": len([d for d in deletes]), "errors": errors}

    def _rescue(self, path, url, env, lost):
        """Fetch target-only commits into refs/ghmirror-backup/<time>/target/. Returns the refs not saved."""
        stamp = time.strftime("%Y%m%dT%H%M%S")
        specs = [f"+{ref}:refs/ghmirror-backup/{stamp}/target/{ref[len('refs/'):]}" for ref in lost]
        run(["fetch", "--quiet", "--no-tags", url, *specs], cwd=path, env=env, check=False)
        unsaved = [ref for ref, sha in lost.items() if not self.has_commit(path, sha)]
        for ref, sha in lost.items():
            if ref not in unsaved:
                log.warning("%s: %s on the target is not on GitHub; commit %s saved under "
                            "refs/ghmirror-backup/%s/target/ before mirroring", path, ref, sha[:10], stamp)
        return unsaved

    def push_refs(self, path, url, env, refspecs):
        if not refspecs:
            return
        p = run(["push", "--porcelain", "--no-verify", url, *refspecs], cwd=path, env=env, check=False)
        if p.returncode != 0:
            raise GitError(f"push {refspecs[:3]} failed: {p.stdout[-500:]} {p.stderr[-500:]}")

    def ref_sha(self, path, ref):
        p = run(["rev-parse", "--verify", "--quiet", ref], cwd=path, check=False)
        return p.stdout.strip() or None
