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
