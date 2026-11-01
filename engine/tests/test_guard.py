import pytest

from ghmirror.config import Config, Target
from ghmirror.engine import Engine, Halted


class FakeGH:
    def __init__(self, repos, owner_ok=True):
        self._repos = repos
        self.owner_ok = owner_ok

    def owner(self):
        from ghmirror.github import NotFound
        if not self.owner_ok:
            raise NotFound("gone")
        return {"login": "acme"}

    def repos(self):
        return self._repos

    def repo(self, full):
        from ghmirror.github import NotFound
        raise NotFound(full)


def make(tmp_path, repos, tracked, owner_ok=True):
    cfg = Config(owner="acme", token="x", targets=[Target("t", "http://gl", "tok", "acme")],
                 database=f"sqlite:///{tmp_path}/g.db", archive_dir=str(tmp_path / "a"), mirrors_dir=str(tmp_path / "m"))
    e = Engine(cfg)
    e.gh = FakeGH(repos, owner_ok)
    e._prepared = True
    for i in tracked:
        e.db.map_put("t", "repo", i, 100 + i, extra={"full_name": f"acme/r{i}"})
    return e


def test_owner_404_halts(tmp_path):
    e = make(tmp_path, [], [1, 2], owner_ok=False)
    with pytest.raises(Halted):
        e.list_repos()
    assert e.guard.halted()
    with pytest.raises(Halted):     # stays halted until reset
        e.list_repos()


def test_mass_disappearance_halts(tmp_path):
    repos = [{"id": i, "name": f"r{i}", "full_name": f"acme/r{i}"} for i in range(1, 51)]
    e = make(tmp_path, repos[:45], range(1, 51))
    with pytest.raises(Halted):
        e.list_repos()


def test_single_deletion_archives_not_halts(tmp_path):
    repos = [{"id": i, "name": f"r{i}", "full_name": f"acme/r{i}"} for i in range(1, 4)]
    e = make(tmp_path, repos[:2], [1, 2, 3])
    archived = []
    e._repo_gone = lambda gh_id, full: archived.append(full)
    assert len(e.list_repos()) == 2
    assert archived == ["acme/r3"] and not e.guard.halted()
