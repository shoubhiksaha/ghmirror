"""Guided setup: `ghmirror init` writes everything docker compose needs into one directory.

    ghmirror.yaml      configuration (no secrets: they are ${VAR} references)
    .env               secrets and compose settings, mode 0600
    identities.yaml    github login -> company email, pre-listed from the organization's members
    secrets/           id_ed25519 (side channel), known_hosts, github-app.pem (App mode)
    prepare-host.sh    the script to run once on each GitLab host

Every question can be answered with a flag, so the same command works unattended. Values already in
an existing .env / ghmirror.yaml are reused, so init can be re-run to add a target or rotate a token.
"""
import base64
import html
import json
import os
import secrets as secrets_mod
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import requests
import yaml

from .server import GIT_EVENTS, META_EVENTS, ORG_EVENTS

HERE = os.path.dirname(__file__)
GITHUB_API = "https://api.github.com"
GITHUB_WEB = "https://github.com"
APP_PERMISSIONS = {"contents": "read", "metadata": "read", "issues": "read", "pull_requests": "read",
                   "discussions": "read", "administration": "read", "members": "read"}
APP_EVENTS = sorted((GIT_EVENTS | META_EVENTS | ORG_EVENTS | {"repository"}) - {"team_add"})
NO_WEBHOOK_SCHEDULE = {"git": 900, "meta": 900}   # polling replaces webhooks: 15-minute lag
TARGET_PREFIX = "ghmirror-target:"


class SetupError(Exception):
    pass


# -- small helpers ---------------------------------------------------------------------------
class Asker:
    def __init__(self, interactive):
        self.interactive = interactive

    def ask(self, question, default=None, flag=None, secret=False):
        if not self.interactive:
            if default is None:
                raise SetupError(f"missing {flag or question} (non-interactive run)")
            return default
        hint = f" [{default}]" if default not in (None, "") and not secret else ""
        while True:
            if secret:
                import getpass
                val = getpass.getpass(f"{question}{hint}: ")
            else:
                val = input(f"{question}{hint}: ")
            val = val.strip() or (default or "")
            if val or default == "":
                return val

    def choose(self, question, options, default):
        if not self.interactive:
            return default
        keys = "/".join(o.upper() if o == default else o for o in options)
        while True:
            val = input(f"{question} ({keys}): ").strip().lower() or default
            if val in options:
                return val


def say(msg=""):
    print(msg, flush=True)


def read_env(path):
    env = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k] = v
    return env


def write_private(path, text, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def env_name(target):
    return "GITLAB_" + "".join(c if c.isalnum() else "_" for c in target.upper()) + "_TOKEN"


def parse_target(text):
    """The line printed by prepare-host.sh -> dict(url, token, ssh, ssh_port, remote, host_keys)."""
    text = text.strip()
    if text.startswith(TARGET_PREFIX):
        text = text[len(TARGET_PREFIX):]
    try:
        d = json.loads(base64.b64decode(text + "=" * (-len(text) % 4)))
    except Exception:
        raise SetupError("that is not a complete ghmirror-target line (cut off or mangled while copying?)")
    for k in ("url", "token"):
        if not d.get(k):
            raise SetupError(f"ghmirror-target line has no {k}")
    d.setdefault("ssh_port", 22)
    d.setdefault("remote", ["sudo"])
    d.setdefault("host_keys", [])
    return d


def side_channel(t):
    if not t.get("ssh"):
        return None
    host = t["ssh"]
    opts = ["ssh", "-i", "/secrets/id_ed25519", "-o", "BatchMode=yes", "-o", "UserKnownHostsFile=/secrets/known_hosts",
            "-o", "StrictHostKeyChecking=yes", "-o", "ServerAliveInterval=30"]
    if int(t.get("ssh_port", 22)) != 22:
        opts += ["-p", str(t["ssh_port"])]
    return opts + [host] + list(t.get("remote") or ["sudo"])


def known_hosts_lines(t):
    if not t.get("ssh"):
        return []
    host = t["ssh"].split("@", 1)[-1]
    port = int(t.get("ssh_port", 22))
    name = host if port == 22 else f"[{host}]:{port}"
    keys = list(t.get("host_keys") or [])
    if not keys and shutil.which("ssh-keyscan"):
        p = subprocess.run(["ssh-keyscan", "-p", str(port), host], capture_output=True, text=True, timeout=30)
        keys = [" ".join(line.split()[1:3]) for line in p.stdout.splitlines() if line and not line.startswith("#")]
        if keys:
            say(f"  host keys of {host} taken from ssh-keyscan (trust on first use)")
    return [f"{name} {k}" for k in keys]


# -- GitHub ----------------------------------------------------------------------------------
def gh_get(path, token=None, bearer=None, api=GITHUB_API):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "ghmirror-init"}
    if token:
        h["Authorization"] = f"token {token}"
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    return requests.get(api + path, headers=h, timeout=30)


def owner_type(owner, token=None):
    r = gh_get(f"/users/{owner}", token)
    if r.status_code == 404:
        raise SetupError(f"GitHub account {owner!r} not found")
    r.raise_for_status()
    return "org" if r.json().get("type") == "Organization" else "user"


def app_manifest(owner, webhook_url, redirect_url):
    m = {"name": f"ghmirror-{owner}"[:34], "url": f"{GITHUB_WEB}/{owner}", "public": False,
         "redirect_url": redirect_url, "default_permissions": APP_PERMISSIONS,
         "default_events": APP_EVENTS if webhook_url else [],
         "description": "Read-only mirror of this account into self-managed GitLab (ghmirror)."}
    m["hook_attributes"] = {"url": webhook_url, "active": True} if webhook_url else \
        {"url": "https://example.invalid/webhook", "active": False}
    return m


def create_app(owner, kind, webhook_url, port, bind, ask):
    """GitHub App manifest flow: two clicks in the browser, no copying of IDs or keys."""
    state = secrets_mod.token_urlsafe(16)
    redirect = f"http://localhost:{port}/callback"
    manifest = app_manifest(owner, webhook_url, redirect)
    action = (f"{GITHUB_WEB}/organizations/{owner}/settings/apps/new" if kind == "org"
              else f"{GITHUB_WEB}/settings/apps/new") + f"?state={state}"
    got = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                body = (f'<!doctype html><title>ghmirror setup</title><body style="font-family:sans-serif">'
                        f'<form id=f method=post action="{html.escape(action)}">'
                        f'<input type=hidden name=manifest value="{html.escape(json.dumps(manifest))}">'
                        f'<p>Creating the GitHub App <b>{html.escape(manifest["name"])}</b>…</p>'
                        f'<button>Continue to GitHub</button></form><script>document.getElementById("f").submit()</script>')
                self._send(200, body)
            elif u.path == "/callback":
                q = parse_qs(u.query)
                if q.get("state", [""])[0] != state or not q.get("code"):
                    return self._send(400, "state mismatch: start again from the link printed by ghmirror init")
                got["code"] = q["code"][0]
                self._send(200, "<p>App created. Return to the terminal: it will send you to the install page.</p>")
            else:
                self._send(404, "not found")

        def _send(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

    srv = HTTPServer((bind, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    start = f"http://localhost:{port}/"
    say(f"\n  1. Open {start} in a browser logged in to GitHub as an owner of {owner}.")
    say(f"     (Engine on a remote server? First run: ssh -L {port}:localhost:{port} <engine-host>)")
    say("  2. Click 'Create GitHub App'. If the page you land on does not load, copy its address and paste it here.")
    try:
        webbrowser.open(start)
    except Exception:
        pass
    pasted = {}
    if ask.interactive:
        def read_paste():
            line = sys.stdin.readline().strip()
            q = parse_qs(urlparse(line).query)
            if q.get("code") and q.get("state", [""])[0] == state:
                pasted["code"] = q["code"][0]
        threading.Thread(target=read_paste, daemon=True).start()
    deadline = time.time() + 1800
    while not (got or pasted) and time.time() < deadline:
        time.sleep(0.5)
    srv.shutdown()
    code = got.get("code") or pasted.get("code")
    if not code:
        raise SetupError("GitHub App was not created within 30 minutes")
    r = requests.post(f"{GITHUB_API}/app-manifests/{code}/conversions",
                      headers={"Accept": "application/vnd.github+json"}, timeout=30)
    if r.status_code != 201:
        raise SetupError(f"GitHub refused the manifest code: {r.status_code} {r.text[:200]}")
    app = r.json()
    say(f"  App {app['slug']} created (id {app['id']}).")
    return app


def wait_installation(app, owner, interactive):
    import jwt
    url = f"{GITHUB_WEB}/apps/{app['slug']}/installations/new"
    say(f"  3. Install it on {owner} (all repositories): {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    deadline = time.time() + (1800 if interactive else 60)
    while time.time() < deadline:
        now = int(time.time())
        tok = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app["id"])}, app["pem"], algorithm="RS256")
        r = gh_get("/app/installations", bearer=tok)
        if r.ok:
            for inst in r.json():
                if inst["account"]["login"].lower() == owner.lower():
                    say(f"  Installed (installation id {inst['id']}).")
                    t = requests.post(f"{GITHUB_API}/app/installations/{inst['id']}/access_tokens", timeout=30,
                                      headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"})
                    return inst["id"], (t.json().get("token") if t.ok else None)
        time.sleep(5)
    raise SetupError(f"the App was not installed on {owner}; install it at {url} and re-run init")


def ensure_org_hook(owner, token, url, secret):
    """Token mode: one organization webhook. Needs admin:org_hook; falls back to polling if refused."""
    h = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    events = sorted(GIT_EVENTS | META_EVENTS | ORG_EVENTS | {"repository"})
    cfg = {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}
    r = requests.get(f"{GITHUB_API}/orgs/{owner}/hooks", headers=h, timeout=30)
    if not r.ok:
        return f"cannot list organization webhooks ({r.status_code}); add one by hand or use App mode"
    hook = next((x for x in r.json() if x.get("config", {}).get("url") == url), None)
    body = {"name": "web", "active": True, "events": events, "config": cfg}
    r = (requests.patch(f"{GITHUB_API}/orgs/{owner}/hooks/{hook['id']}", headers=h, json=body, timeout=30) if hook
         else requests.post(f"{GITHUB_API}/orgs/{owner}/hooks", headers=h, json=body, timeout=30))
    return None if r.ok else f"creating the webhook failed ({r.status_code} {r.text[:120]})"


# -- GitLab ----------------------------------------------------------------------------------
def check_gitlab(t):
    try:
        r = requests.get(t["url"].rstrip("/") + "/api/v4/user", headers={"PRIVATE-TOKEN": t["token"]}, timeout=20)
    except requests.RequestException as e:
        return f"not reachable from here ({e.__class__.__name__}); check again with 'ghmirror check'"
    if r.status_code == 401:
        raise SetupError(f"{t['url']}: token rejected")
    if r.ok and not r.json().get("is_admin"):
        raise SetupError(f"{t['url']}: the token's user {r.json().get('username')} is not an administrator")
    return None if r.ok else f"unexpected answer {r.status_code}"


# -- the wizard ------------------------------------------------------------------------------
def init(a):
    d = os.path.abspath(a.dir)
    os.makedirs(os.path.join(d, "secrets"), exist_ok=True)
    os.chmod(os.path.join(d, "secrets"), 0o700)
    ask = Asker(sys.stdin.isatty() and not a.yes)
    env = read_env(os.path.join(d, ".env"))
    old = {}
    if os.path.exists(os.path.join(d, "ghmirror.yaml")):
        with open(os.path.join(d, "ghmirror.yaml")) as f:
            old = yaml.safe_load(f) or {}
    say("ghmirror setup: answers are saved in " + d + "\n")

    # SSH key for the side channel, and the host script that authorizes it
    key = os.path.join(d, "secrets", "id_ed25519")
    if not os.path.exists(key):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ghmirror-engine", "-f", key], check=True)
    with open(key + ".pub") as f:
        pub = f.read().strip()
    shutil.copy(os.path.join(HERE, "prepare-host.sh"), os.path.join(d, "prepare-host.sh"))

    # 1. GitHub
    owner = a.owner or ask.ask("GitHub organization (or user) to mirror",
                                (old.get("source") or {}).get("owner"), "--owner")
    token = a.github_token or env.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    kind = a.owner_type or owner_type(owner, token)
    webhook = a.webhook_url
    if webhook is None:
        webhook = ask.ask("Public HTTPS URL that GitHub can reach for webhooks (Enter = none, poll every 15 min)",
                          env.get("GHM_WEBHOOK_URL", ""), "--webhook-url")
    if webhook and not webhook.rstrip("/").endswith("/webhook"):
        webhook = webhook.rstrip("/") + "/webhook"
    hook_secret = env.get("GITHUB_WEBHOOK_SECRET") or secrets_mod.token_hex(32)
    auth_mode = a.github_auth or ("token" if token and not env.get("GITHUB_APP_ID") else
                                  "app" if env.get("GITHUB_APP_ID") else None)
    if not auth_mode:
        say("\nGitHub access: 'app' creates a read-only GitHub App for you (recommended, 2 clicks);"
            "\n               'token' uses a classic personal access token (repo, read:org; admin:org_hook for webhooks).")
        auth_mode = ask.choose("Which", ["app", "token"], "app")
    auth = {}
    if auth_mode == "app":
        if env.get("GITHUB_APP_ID") and os.path.exists(os.path.join(d, "secrets", "github-app.pem")) and not a.new_app:
            say(f"GitHub App {env['GITHUB_APP_ID']} already configured (use --new-app to create another).")
            auth = {"GITHUB_APP_ID": env["GITHUB_APP_ID"], "GITHUB_APP_INSTALLATION_ID": env["GITHUB_APP_INSTALLATION_ID"]}
        else:
            app = create_app(owner, kind, webhook, a.callback_port, a.callback_bind, ask)
            write_private(os.path.join(d, "secrets", "github-app.pem"), app["pem"], 0o600)
            hook_secret = app.get("webhook_secret") or hook_secret
            inst, inst_token = wait_installation(app, owner, ask.interactive)
            token = token or inst_token   # short-lived; used below only to list the members
            auth = {"GITHUB_APP_ID": str(app["id"]), "GITHUB_APP_INSTALLATION_ID": str(inst)}
    else:
        token = token or ask.ask("GitHub token", None, "--github-token", secret=True)
        r = gh_get("/user", token)
        if r.status_code == 401:
            raise SetupError("GitHub token rejected")
        say(f"GitHub: token of {r.json().get('login')}, scopes: {r.headers.get('X-OAuth-Scopes') or 'fine-grained'}")
        auth = {"GITHUB_TOKEN": token}
        if webhook and kind == "org":
            err = ensure_org_hook(owner, token, webhook, hook_secret)
            say(f"  webhook: {err}" if err else f"  organization webhook -> {webhook}")
        elif webhook:
            say("  webhook: personal accounts have no account-wide webhook; polling every 15 minutes instead")
            webhook = ""

    # 2. GitLab targets
    targets = []
    names = a.target_names or [t["name"] for t in old.get("targets") or []] or ["cloud", "offline"]
    pasted = dict(x.split("=", 1) for x in (a.target or []))
    old_t = {t["name"]: t for t in old.get("targets") or []}
    script = os.path.join(os.environ.get("GHM_HOST_DIR") or d, "prepare-host.sh")   # path as the operator sees it
    say("\nGitLab targets. Prepare each GitLab host once, from here (with an account that may sudo there):\n\n"
        f"  ssh <admin>@<gitlab-host> sudo bash -s -- --pubkey \"{shlex.quote(pub)}\" < {script}\n\n"
        f"  or copy {script} to the host and run there: sudo bash prepare-host.sh --pubkey {shlex.quote(pub)}"
        "\n\n  It prints a 'ghmirror-target:' line. Docker-hosted GitLab: add --container <name> if there are several."
        "\n  Host sshd not on 22, or a different address for the engine: add --ssh-port / --ssh-host.\n")
    for name in names:
        line = pasted.get(name)
        if not line and name in old_t and env.get(env_name(name)) and not a.yes:
            keep = ask.choose(f"Keep the existing settings for {name}", ["y", "n"], "y")
            if keep == "y":
                targets.append(("keep", name, old_t[name]))
                continue
        if not line and name in old_t and a.yes:
            targets.append(("keep", name, old_t[name]))
            continue
        while True:
            line = line or ask.ask(f"Paste the ghmirror-target line for '{name}' (or 'skip')", None, f"--target {name}=...")
            if line == "skip":
                break
            try:
                t = parse_target(line)
                break
            except SetupError as e:
                if not ask.interactive:
                    raise
                say(f"  {e}: paste the whole line, starting with {TARGET_PREFIX}")
                line = None
        if line == "skip":
            continue
        problem = check_gitlab(t)
        say(f"  {name}: {t['url']}" + (f"  ({problem})" if problem else "  token ok, admin"))
        targets.append(("new", name, t))
    if not targets:
        raise SetupError("at least one GitLab target is needed")

    # 3. identities, alerts
    if not os.path.exists(os.path.join(d, "identities.yaml")):
        members = []
        try:
            h = {"Authorization": f"token {token}"} if token else {}
            r = requests.get(f"{GITHUB_API}/orgs/{owner}/members?per_page=100", headers=h, timeout=30) \
                if kind == "org" and h else None
            members = [m["login"] for m in r.json()] if r is not None and r.ok else []
        except requests.RequestException:
            pass
        lines = ["# GitHub login -> company email. Used for the mirror accounts and to link SSO at failover.",
                 "# Uncomment and fill in one line per person; anyone missing gets a placeholder address."]
        lines += [f"# {m}: {m}@company.example" for m in sorted(members, key=str.lower)] or ["# alice-gh: alice@company.example"]
        with open(os.path.join(d, "identities.yaml"), "w") as f:
            f.write("\n".join(lines) + "\n")
    alert = a.alert_webhook if a.alert_webhook is not None else \
        ask.ask("Slack-compatible alert webhook URL (Enter = none)", env.get("ALERT_WEBHOOK_URL", ""), "--alert-webhook")

    # 4. write files
    new_env = {k: v for k, v in env.items()}
    new_env.update(auth)
    if auth_mode == "app":
        new_env.pop("GITHUB_TOKEN", None)
    else:
        new_env.pop("GITHUB_APP_ID", None)
        new_env.pop("GITHUB_APP_INSTALLATION_ID", None)
    new_env.setdefault("POSTGRES_PASSWORD", secrets_mod.token_hex(16))
    new_env.setdefault("GITHUB_MIGRATION_TOKEN", "")   # optional classic PAT (repo, admin:org): weekly archive
    new_env["GITHUB_WEBHOOK_SECRET"] = hook_secret
    new_env["GHM_WEBHOOK_URL"] = webhook
    new_env["ALERT_WEBHOOK_URL"] = alert
    kh = []
    targets_yaml = []
    # GitLab users and groups share one namespace: a personal account's group must not take the person's
    # own username, or their mirror account becomes "<login>-gh" and @mentions stop matching
    group = a.group or (old.get("targets") or [{}])[0].get("group") or (owner if kind == "org" else f"{owner}-github")
    for how, name, t in targets:
        if how == "keep":
            targets_yaml.append(t)
            continue
        new_env[env_name(name)] = t["token"]
        kh += known_hosts_lines(t)
        entry = {"name": name, "url": t["url"], "token": "${%s}" % env_name(name), "group": group,
                 "visibility_cap": "private", "mirror_url": t["url"]}
        sc = side_channel(t)
        if sc:
            entry["side_channel"] = sc
        targets_yaml.append(entry)
    khf = os.path.join(d, "secrets", "known_hosts")
    existing = open(khf).read().splitlines() if os.path.exists(khf) else []
    write_private(khf, "\n".join(dict.fromkeys(existing + kh)) + "\n", 0o644)
    write_private(os.path.join(d, ".env"),
                  "# written by ghmirror init: secrets, keep private\n" +
                  "".join(f"{k}={v}\n" for k, v in new_env.items()), 0o600)
    src_auth = ({"app_id": "${GITHUB_APP_ID}", "app_private_key_file": "/secrets/github-app.pem",
                 "app_installation_id": "${GITHUB_APP_INSTALLATION_ID}"} if auth_mode == "app"
                else {"token": "${GITHUB_TOKEN}"})
    src_auth["migration_token"] = "${GITHUB_MIGRATION_TOKEN:-}"   # optional, for the weekly migration archive
    cfg = {
        "source": {"owner": owner, "owner_type": kind, "auth": src_auth, "webhook_secret": "${GITHUB_WEBHOOK_SECRET}",
                   "repos": (old.get("source") or {}).get("repos") or {"include": a.include or ["*"], "exclude": []}},
        "targets": targets_yaml,
        "state": {"database": "${DATABASE_URL:-sqlite:////data/ghmirror.db}", "mirrors_dir": "/data/mirrors",
                  "archive_dir": "/data/archive"},
        "identity": {"email_domain": (old.get("identity") or {}).get("email_domain") or f"{owner.lower()}.github-mirror.invalid",
                     "email_map_file": "identities.yaml", "import_keys": True},
        "guard": old.get("guard") or {"max_disappear_ratio": 0.02, "max_disappear_count": 1},
        "alerts": {"webhook_url": "${ALERT_WEBHOOK_URL:-}"},
        "server": {"listen": "0.0.0.0:8080", "workers": 4},
        "redirect": {"target": targets_yaml[0]["name"], "base_url": "https://git.example.com"},
    }
    if a.include:
        cfg["source"]["repos"] = {"include": a.include, "exclude": []}
    if not webhook:
        cfg["schedule"] = dict(NO_WEBHOOK_SCHEDULE)
    elif old.get("schedule"):
        cfg["schedule"] = old["schedule"]
    with open(os.path.join(d, "ghmirror.yaml"), "w") as f:
        f.write("# Written by `ghmirror init`; edit freely, re-running init keeps your changes to repos/guard/identity.\n"
                "# Reference: ghmirror.example.yaml and the guide, appendix B.\n")
        yaml.safe_dump(cfg, f, sort_keys=False, width=200)
    os.chmod(os.path.join(d, "ghmirror.yaml"), 0o644)

    say(f"\nWrote {d}/ghmirror.yaml, .env, identities.yaml, secrets/.")
    if webhook:
        say(f"Webhooks go to {webhook}: route that path to the engine's port 8080 (reverse proxy or tunnel).")
    say("Next: fill in identities.yaml when you can (needed before a failover, not before mirroring).")
    return 0
