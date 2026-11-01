import argparse
import base64
import json
import os
import shlex
import stat
from unittest import mock

import yaml

from ghmirror import config, setup
from ghmirror.sidechannel import SideChannel


def target_line(**over):
    d = {"url": "https://gl.example.com", "token": "glpat-x", "ssh": "ghmirror@gl.example.com", "ssh_port": 22,
         "remote": ["sudo", "docker", "exec", "-i", "gitlab"], "host_keys": ["ssh-ed25519 AAAAkey"]}
    d.update(over)
    return setup.TARGET_PREFIX + base64.b64encode(json.dumps(d).encode()).decode()


def test_parse_target_and_side_channel():
    t = setup.parse_target(target_line())
    assert t["url"] == "https://gl.example.com" and t["token"] == "glpat-x"
    sc = setup.side_channel(t)
    assert sc[0] == "ssh" and "ghmirror@gl.example.com" in sc and sc[-5:] == ["sudo", "docker", "exec", "-i", "gitlab"]
    assert "UserKnownHostsFile=/secrets/known_hosts" in sc and "-p" not in sc
    assert setup.known_hosts_lines(t) == ["gl.example.com ssh-ed25519 AAAAkey"]
    t2 = setup.parse_target(target_line(ssh_port=2200))
    assert setup.side_channel(t2)[-8:-6] == ["-p", "2200"]
    assert setup.known_hosts_lines(t2) == ["[gl.example.com]:2200 ssh-ed25519 AAAAkey"]


def test_ssh_side_channel_quotes_remote_command():
    sc = SideChannel(["ssh", "-i", "k", "ghmirror@h", "sudo", "docker", "exec", "-i", "gitlab"])
    cmd = sc._cmd(["sh", "-c", "cat > /tmp/ghmirror-agent.rb"])
    # one remote string: the redirect runs inside `sh -c` in the container, not in the login shell
    assert cmd[-1] == "sh -c 'cat > /tmp/ghmirror-agent.rb'"
    assert shlex.split(cmd[-1]) == ["sh", "-c", "cat > /tmp/ghmirror-agent.rb"]
    local = SideChannel(["docker", "exec", "-i", "gitlab"])
    assert local._cmd(["sh", "-c", "x"]) == ["docker", "exec", "-i", "gitlab", "sh", "-c", "x"]


def test_manifest_events_and_permissions():
    m = setup.app_manifest("acme", "https://m.acme.com/webhook", "http://localhost:8765/callback")
    assert m["hook_attributes"] == {"url": "https://m.acme.com/webhook", "active": True}
    assert {"push", "pull_request", "issues", "repository", "member"} <= set(m["default_events"])
    assert all(v == "read" for v in m["default_permissions"].values())
    quiet = setup.app_manifest("acme", "", "http://localhost:8765/callback")
    assert quiet["hook_attributes"]["active"] is False and quiet["default_events"] == []


def fake_get(path, token=None, bearer=None, api=None):
    r = mock.Mock(ok=True, status_code=200, headers={"X-OAuth-Scopes": "repo, read:org"})
    r.json.return_value = {"type": "User", "login": "alice"}
    return r


def test_init_unattended(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "gh_get", fake_get)
    monkeypatch.setattr(setup, "check_gitlab", lambda t: None)
    args = argparse.Namespace(dir=str(tmp_path), owner="alice", owner_type=None, github_auth="token",
                              github_token="ghp_test", new_app=False, webhook_url="", target_names=["cloud", "offline"],
                              target=[f"cloud={target_line()}", f"offline={target_line(url='https://lan.example', remote=['sudo'])}"],
                              group=None, include=None, alert_webhook="", callback_port=8765,
                              callback_bind="127.0.0.1", yes=True)
    assert setup.init(args) == 0
    env = setup.read_env(tmp_path / ".env")
    assert env["GITHUB_TOKEN"] == "ghp_test" and env["GITLAB_CLOUD_TOKEN"] == "glpat-x"
    assert len(env["POSTGRES_PASSWORD"]) == 32 and len(env["GITHUB_WEBHOOK_SECRET"]) == 64
    assert stat.S_IMODE(os.stat(tmp_path / ".env").st_mode) == 0o600
    assert (tmp_path / "secrets" / "id_ed25519").exists() and (tmp_path / "prepare-host.sh").exists()
    raw = yaml.safe_load(open(tmp_path / "ghmirror.yaml"))
    assert raw["targets"][1]["side_channel"][-1] == "sudo" and raw["schedule"] == setup.NO_WEBHOOK_SCHEDULE
    # the written config loads (the engine's own loader, env from .env)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = config.load(str(tmp_path / "ghmirror.yaml"))
    assert cfg.owner == "alice" and cfg.owner_type == "user" and [t.name for t in cfg.targets] == ["cloud", "offline"]
    assert cfg.targets[0].token == "glpat-x" and cfg.schedule["git"] == 900
    # re-running keeps secrets and targets
    pw = env["POSTGRES_PASSWORD"]
    args.target = None
    assert setup.init(args) == 0
    assert setup.read_env(tmp_path / ".env")["POSTGRES_PASSWORD"] == pw
    assert len(yaml.safe_load(open(tmp_path / "ghmirror.yaml"))["targets"]) == 2


def test_app_manifest_flow(monkeypatch):
    import html as html_mod
    import re
    import socket
    import threading
    import urllib.parse
    import urllib.request

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(setup.webbrowser, "open", lambda *a: None)
    posted = {}

    def fake_post(url, **kw):
        posted["url"] = url
        return mock.Mock(status_code=201, json=lambda: {"id": 42, "slug": "ghmirror-acme", "pem": "PEM",
                                                        "webhook_secret": "whsec"})
    monkeypatch.setattr(setup.requests, "post", fake_post)
    out = {}
    t = threading.Thread(target=lambda: out.update(app=setup.create_app(
        "acme", "org", "https://m.acme.com/webhook", port, "127.0.0.1", setup.Asker(False))))
    t.start()
    for _ in range(50):
        try:
            page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
            break
        except OSError:
            import time
            time.sleep(0.1)
    action = html_mod.unescape(re.search(r'action="([^"]+)"', page).group(1))
    assert action.startswith("https://github.com/organizations/acme/settings/apps/new?state=")
    manifest = json.loads(html_mod.unescape(re.search(r'name=manifest value="([^"]+)"', page).group(1)))
    assert manifest["redirect_url"] == f"http://localhost:{port}/callback"
    state = urllib.parse.parse_qs(urllib.parse.urlparse(action).query)["state"][0]
    try:   # wrong state is refused
        urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=x&state=forged")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 400
    urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=abc&state={state}").read()
    t.join(10)
    assert out["app"]["id"] == 42 and posted["url"].endswith("/app-manifests/abc/conversions")
