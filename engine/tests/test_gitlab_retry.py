"""A POST whose outcome is unknown must not be blindly resent: that is how duplicates are made."""
import pytest
import requests

from ghmirror import gitlab
from ghmirror.config import Target


class Resp:
    def __init__(self, status, body=b"{}"):
        self.status_code = status
        self.content = body
        self.text = body.decode()
        self.headers = {}

    def json(self):
        import json
        return json.loads(self.content)


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.headers = {}

    def request(self, *a, **kw):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


@pytest.fixture
def gl(monkeypatch):
    monkeypatch.setattr(gitlab.time, "sleep", lambda s: None)
    return gitlab.GitLab(Target(name="t", url="http://gl", token="x", group="g"))


@pytest.mark.parametrize("first", [Resp(502), Resp(504), Resp(500), requests.ReadTimeout("slow")])
def test_ambiguous_post_is_recovered_not_resent(gl, first):
    gl.s = FakeSession([first, Resp(201, b'{"id": 2}')])
    got = gl.post("/projects/1/issues", json={}, recover=lambda: {"id": 1, "iid": 7})
    assert got == {"id": 1, "iid": 7}
    assert gl.s.calls == 1


def test_ambiguous_post_resent_when_object_absent(gl):
    gl.s = FakeSession([Resp(502), Resp(201, b'{"id": 2}')])
    assert gl.post("/x", json={}, recover=lambda: None) == {"id": 2}
    assert gl.s.calls == 2


@pytest.mark.parametrize("first", [Resp(503), Resp(429), requests.ConnectTimeout("never sent")])
def test_refused_post_resent_without_lookup(gl, first):
    looked = []
    gl.s = FakeSession([first, Resp(201, b'{"id": 2}')])
    assert gl.post("/x", json={}, recover=lambda: looked.append(1)) == {"id": 2}
    assert looked == []


def test_failed_lookup_falls_back_to_resend(gl):
    def boom():
        raise gitlab.GLError(500, "lookup broke")
    gl.s = FakeSession([Resp(502), Resp(201, b'{"id": 3}')])
    assert gl.post("/x", json={}, recover=boom) == {"id": 3}


def test_get_never_uses_recover(gl):
    gl.s = FakeSession([Resp(502), Resp(200, b'{"ok": 1}')])
    assert gl.get("/x", recover=lambda: {"bad": 1}) == {"ok": 1}


def test_conflict_on_resend_after_late_landing_is_adopted(gl):
    # the first attempt timed out at the proxy, GitLab finished it after the lookup ran
    seen = iter([None, None, {"id": 9}])
    gl.s = FakeSession([Resp(502), Resp(400, b'{"message": {"path": ["has already been taken"]}}')])
    assert gl.post("/projects", json={}, recover=lambda: next(seen)) == {"id": 9}


def test_plain_conflict_is_not_masked(gl):
    gl.s = FakeSession([Resp(400, b'{"message": "taken"}')])
    with pytest.raises(gitlab.GLError):
        gl.post("/projects", json={}, recover=lambda: {"id": 1})
