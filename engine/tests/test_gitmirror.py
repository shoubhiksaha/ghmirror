import os
import subprocess

from ghmirror.gitmirror import Mirror


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_push_saves_target_only_commits_before_deleting(tmp_path):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    work, mirror, target = tmp_path / "w", tmp_path / "m.git", tmp_path / "t.git"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "a").write_text("1")
    git(work, "add", "a")
    subprocess.run(["git", "commit", "-qm", "one"], cwd=work, env=env, check=True)
    git(tmp_path, "clone", "-q", "--mirror", str(work), str(mirror))
    git(tmp_path, "clone", "-q", "--mirror", str(work), str(target))
    # someone commits on the target only, on a new branch
    git(work, "checkout", "-q", "-b", "local-work")
    (work / "b").write_text("2")
    git(work, "add", "b")
    subprocess.run(["git", "commit", "-qm", "made on the mirror"], cwd=work, env=env, check=True)
    only_on_target = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", str(target), "local-work")

    gm = Mirror.__new__(Mirror)
    res = gm.push(str(mirror), str(target), dict(os.environ))
    assert res["errors"] == [] and res["deleted"] == 1
    assert "refs/heads/local-work" not in git(target, "for-each-ref", "--format=%(refname)")   # refs identical again
    saved = git(mirror, "for-each-ref", "--format=%(refname) %(objectname)", "refs/ghmirror-backup/")
    assert saved.endswith(f"/target/heads/local-work {only_on_target}")                         # ... and nothing lost


class _GH:
    def __init__(self, url):
        self.url = url

    def git_env(self):
        return {}

    def clone_url(self, full, wiki=False):
        return self.url


def test_git_notes_are_mirrored_and_old_mirrors_start_fetching_them(tmp_path):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    work, target = tmp_path / "w", tmp_path / "t.git"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "a").write_text("1")
    git(work, "add", "a")
    subprocess.run(["git", "commit", "-qm", "one"], cwd=work, env=env, check=True)
    subprocess.run(["git", "notes", "add", "-m", "applied from list"], cwd=work, env=env, check=True)
    git(tmp_path, "init", "-q", "--bare", str(target))

    gm = Mirror(tmp_path / "mirrors", _GH(str(work)))
    path = gm.fetch("o/r")
    assert gm.push(path, str(target), dict(os.environ))["errors"] == []
    assert git(target, "rev-parse", "refs/notes/commits") == git(work, "rev-parse", "refs/notes/commits")

    # a mirror made by an older version: no refs/notes/* refspec, so its notes never moved
    git(path, "config", "--unset-all", "remote.origin.fetch", "refs/notes")
    subprocess.run(["git", "notes", "append", "-m", "second note"], cwd=work, env=env, check=True)
    gm.fetch("o/r")
    assert git(path, "rev-parse", "refs/notes/commits") == git(work, "rev-parse", "refs/notes/commits")
    assert gm.push(path, str(target), dict(os.environ))["updated"] == 1
    assert git(target, "rev-parse", "refs/notes/commits") == git(work, "rev-parse", "refs/notes/commits")
