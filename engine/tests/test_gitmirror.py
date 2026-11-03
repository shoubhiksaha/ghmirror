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
