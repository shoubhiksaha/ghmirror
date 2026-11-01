"""Configuration loading. YAML with ${ENV_VAR} interpolation; secrets come from the environment."""
import os
import re
from dataclasses import dataclass, field

import yaml

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1)) or m.group(2) or "", value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


@dataclass
class Target:
    name: str
    url: str
    token: str
    group: str
    side_channel: list | None = None      # command prefix that runs a program on the GitLab host (see sidechannel.py)
    visibility_cap: str = "private"       # most open visibility a mirrored project may get
    ci_config_path: str | None = None     # e.g. "pipelines/{repo}.gitlab-ci.yml@{group}/ci-config"
    git_url: str | None = None            # override for git over HTTP(S) if different from url
    mirror_url: str | None = None         # how the OTHER GitLab servers reach this one (push mirrors after promote)

    @property
    def api(self):
        return self.url.rstrip("/") + "/api/v4"


@dataclass
class Config:
    owner: str
    owner_type: str = "org"
    api_url: str = "https://api.github.com"
    web_url: str = "https://github.com"
    token: str | None = None
    app_id: str | None = None
    app_private_key: str | None = None
    app_installation_id: str | None = None
    migration_token: str | None = None     # classic PAT for the migrations API (refuses App/OAuth tokens)
    webhook_secret: str = ""
    include: list = field(default_factory=lambda: ["*"])
    exclude: list = field(default_factory=list)
    targets: list = field(default_factory=list)
    database: str = "sqlite:///ghmirror.db"
    mirrors_dir: str = "mirrors"
    archive_dir: str = "archive"
    email_domain: str = "github.mirror.invalid"
    email_map: dict = field(default_factory=dict)     # github login -> real company email
    import_keys: bool = True
    import_avatars: bool = True
    mirror_attachments: bool = True
    max_disappear_ratio: float = 0.02
    max_disappear_count: int = 1
    schedule: dict = field(default_factory=dict)
    alert_webhook: str | None = None
    listen: str = "0.0.0.0:8080"
    workers: int = 2
    redirect_target: str | None = None
    redirect_base_url: str | None = None
    broadcast_message: str = "This GitLab is now the primary code host. Runbook: see docs/RUNBOOK.md"

    def target(self, name):
        for t in self.targets:
            if t.name == name:
                return t
        raise KeyError(f"unknown target {name!r}")


SCHEDULE_DEFAULTS = {
    "repos": 3600,          # re-list repositories (new / deleted / renamed)
    "git": 21600,           # git fetch + push for every repository
    "meta": 3600,           # incremental issues/PRs/comments since cursor
    "full": 86400,          # full metadata pass incl. deletions and reactions
    "org": 21600,           # members and teams
    "verify": 86400,        # parity report
    "migration": 604800,    # GitHub migration archive (L2 "nuclear" copy)
}


def load(path):
    with open(path) as f:
        raw = _expand(yaml.safe_load(f)) or {}
    src = raw.get("source", {})
    auth = src.get("auth", {})
    state = raw.get("state", {})
    ident = raw.get("identity", {})
    guard = raw.get("guard", {})
    server = raw.get("server", {})
    redirect = raw.get("redirect", {})
    email_map = dict(ident.get("email_map") or {})
    if ident.get("email_map_file"):
        with open(os.path.join(os.path.dirname(os.path.abspath(path)), ident["email_map_file"])) as f:
            email_map.update(yaml.safe_load(f) or {})
    key = auth.get("app_private_key")
    if auth.get("app_private_key_file"):
        with open(auth["app_private_key_file"]) as f:
            key = f.read()
    targets = [Target(name=t["name"], url=t["url"].rstrip("/"), token=t["token"], group=t["group"],
                      side_channel=t.get("side_channel"), visibility_cap=t.get("visibility_cap", "private"),
                      ci_config_path=t.get("ci_config_path"), git_url=t.get("git_url"),
                      mirror_url=t.get("mirror_url"))
               for t in raw.get("targets", [])]
    if not targets:
        raise ValueError("config: at least one target is required")
    cfg = Config(
        owner=src["owner"], owner_type=src.get("owner_type", "org"),
        api_url=src.get("api_url", "https://api.github.com").rstrip("/"),
        web_url=src.get("web_url", "https://github.com").rstrip("/"),
        token=auth.get("token"), app_id=auth.get("app_id"), app_private_key=key,
        app_installation_id=auth.get("app_installation_id"), migration_token=auth.get("migration_token") or None,
        webhook_secret=src.get("webhook_secret", ""),
        include=src.get("repos", {}).get("include", ["*"]), exclude=src.get("repos", {}).get("exclude", []),
        targets=targets,
        database=state.get("database", "sqlite:///ghmirror.db"),
        mirrors_dir=state.get("mirrors_dir", "mirrors"), archive_dir=state.get("archive_dir", "archive"),
        email_domain=ident.get("email_domain", "github.mirror.invalid"), email_map=email_map,
        import_keys=ident.get("import_keys", True), import_avatars=ident.get("import_avatars", True),
        mirror_attachments=raw.get("sync", {}).get("mirror_attachments", True),
        max_disappear_ratio=guard.get("max_disappear_ratio", 0.02),
        max_disappear_count=guard.get("max_disappear_count", 1),
        schedule={**SCHEDULE_DEFAULTS, **(raw.get("schedule") or {})},
        alert_webhook=raw.get("alerts", {}).get("webhook_url") or None,
        listen=server.get("listen", "0.0.0.0:8080"), workers=int(server.get("workers", 2)),
        redirect_target=redirect.get("target"), redirect_base_url=redirect.get("base_url"),
        broadcast_message=raw.get("promote", {}).get("broadcast_message", Config.broadcast_message),
    )
    if not (cfg.token or (cfg.app_id and cfg.app_private_key and cfg.app_installation_id)):
        raise ValueError("config: source.auth needs either token or app_id + app_private_key(_file) + app_installation_id")
    return cfg
