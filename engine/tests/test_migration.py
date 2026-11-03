from types import SimpleNamespace

from ghmirror import migration


class FakeDB(dict):
    def get(self, k, default=None):
        return super().get(k, default)

    def set(self, k, v):
        self[k] = v


def make_eng(token=None, migration_token=None, refuse=True):
    alerts = []

    def start(repos):
        raise RuntimeError("GitHub POST https://api.github.com/user/migrations: 403 Forbidden")
    gh = SimpleNamespace(repos=lambda: [{"full_name": "a/b"}], start_migration=start)
    return SimpleNamespace(cfg=SimpleNamespace(token=token, migration_token=migration_token), gh=gh, db=FakeDB(),
                           selected=lambda r: True, alert=lambda lvl, msg: alerts.append(msg)), alerts


def test_app_without_migration_token_skips_quietly():
    eng, alerts = make_eng()
    assert migration.run_migration(eng) == {"state": "skipped"} and alerts == []


def test_refusal_alerts_once():
    eng, alerts = make_eng(token="gho_oauth")
    for _ in range(3):
        assert migration.run_migration(eng)["state"] == "unauthorized"
    assert len(alerts) == 1 and "migration_token" in alerts[0]


def test_verify_markdown_report():
    from ghmirror.verify import render_markdown
    rep = {"ts": 0, "owner": "acme", "targets": ["cloud"], "repos": ["acme/app"], "repair": False,
           "checked": {"repos": 1, "issues": 3, "mrs": 2, "pending": 0}, "drift_count": 0, "drift": []}
    md = render_markdown(rep)
    assert "**PASS**" in md and "| 1 | 3 | 2 | 0 |" in md and "None." in md
    rep["drift"] = [{"target": "cloud", "repo": "acme/app", "kind": "issue", "id": 2, "field": "title",
                     "github": "Login fails", "gitlab": "a|b"}]
    rep["drift_count"] = 1
    md = render_markdown(rep)
    assert "**DRIFT**: 1" in md and "| cloud | `acme/app` | issue #2 | title | `Login fails` | `a\\|b` |" in md


def test_archive_ignores_volatile_fields_of_embedded_repos(tmp_path):
    from ghmirror.archive import Archive
    a = Archive(str(tmp_path))
    pr = {"number": 4, "title": "Squash PR", "base": {"repo": {"full_name": "o/r", "pushed_at": "t1", "updated_at": "t1"}}}
    assert a.put("o/r", "pulls", 4, pr)
    pr2 = {**pr, "base": {"repo": {"full_name": "o/r", "pushed_at": "t2", "updated_at": "t2"}}}
    assert not a.put("o/r", "pulls", 4, pr2)                 # someone pushed to the repo: not a new PR version
    assert a.put("o/r", "pulls", 4, {**pr2, "title": "Renamed"})   # a real change is archived
    assert len(list((tmp_path / "o" / "r" / "pulls" / "_history" / "4").iterdir())) == 1
    repo = {"full_name": "o/r", "pushed_at": "t1"}
    assert a.put("o/r", "repo", "repo", repo) and a.put("o/r", "repo", "repo", {**repo, "pushed_at": "t2"})  # top level: kept
