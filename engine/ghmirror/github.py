"""GitHub source client: REST + GraphQL, token or GitHub App auth, rate-limit aware."""
import base64
import logging
import threading
import time

import requests

log = logging.getLogger("ghmirror.github")


class NotFound(Exception):
    pass


class SourceUnavailable(Exception):
    """Credentials rejected, account suspended or API unreachable: never treat as 'deleted'."""


class GitHub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.api = cfg.api_url
        self.web = cfg.web_url
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                               "User-Agent": "ghmirror"})
        self._tok = cfg.token
        self._tok_exp = float("inf") if cfg.token else 0
        self._lock = threading.Lock()

    # -- auth --------------------------------------------------------------------
    def token(self):
        with self._lock:
            if time.time() < self._tok_exp - 300:
                return self._tok
            import jwt
            now = int(time.time())
            app_jwt = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.cfg.app_id)},
                                 self.cfg.app_private_key, algorithm="RS256")
            r = requests.post(f"{self.api}/app/installations/{self.cfg.app_installation_id}/access_tokens",
                              headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"},
                              timeout=30)
            if r.status_code >= 400:
                raise SourceUnavailable(f"GitHub App token exchange failed: {r.status_code} {r.text[:200]}")
            self._tok = r.json()["token"]
            self._tok_exp = time.time() + 3600
            return self._tok

    def git_env(self):
        """Environment that authenticates git over HTTPS to github without putting the token in argv/URLs."""
        cred = base64.b64encode(f"x-access-token:{self.token()}".encode()).decode()
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{self.web}/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {cred}", "GIT_TERMINAL_PROMPT": "0"}

    def clone_url(self, full_name, wiki=False):
        return f"{self.web}/{full_name}{'.wiki' if wiki else ''}.git"

    # -- transport -------------------------------------------------------------------
    def request(self, method, path, params=None, json=None, accept=None, stream=False, allow_redirects=True,
                token=None):
        url = path if path.startswith("http") else self.api + path
        headers = {"Authorization": f"Bearer {token or self.token()}"}
        if accept:
            headers["Accept"] = accept
        for attempt in range(8):
            try:
                r = self.s.request(method, url, params=params, json=json, headers=headers, timeout=120,
                                   stream=stream, allow_redirects=allow_redirects)
            except requests.RequestException as e:
                if attempt == 7:
                    raise SourceUnavailable(f"GitHub unreachable: {e}")
                time.sleep(min(60, 2 ** attempt))
                continue
            if r.status_code in (403, 429) and (r.headers.get("Retry-After") or
                                                r.headers.get("X-RateLimit-Remaining") == "0"):
                wait = int(r.headers.get("Retry-After") or
                           max(1, int(r.headers.get("X-RateLimit-Reset", time.time() + 60)) - int(time.time())))
                log.warning("GitHub rate limited, sleeping %ss", wait)
                time.sleep(min(wait + 1, 3600))
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(min(60, 2 ** attempt))
                continue
            if r.status_code == 404 or r.status_code == 410:
                raise NotFound(f"{method} {url}")
            if r.status_code in (401,) or (r.status_code == 403 and "suspended" in r.text.lower()):
                raise SourceUnavailable(f"{method} {url}: {r.status_code} {r.text[:200]}")
            if r.status_code >= 400:
                raise RuntimeError(f"GitHub {method} {url}: {r.status_code} {r.text[:300]}")
            return r
        raise SourceUnavailable(f"GitHub {method} {url}: retries exhausted")

    def get(self, path, params=None, accept=None):
        return self.request("GET", path, params=params, accept=accept).json()

    def paginate(self, path, params=None, accept=None):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        url = path
        while url:
            r = self.request("GET", url, params=params, accept=accept)
            data = r.json()
            if isinstance(data, dict):  # e.g. {"total_count":..,"repositories":[..]}
                data = next((v for v in data.values() if isinstance(v, list)), [])
            yield from data
            url = r.links.get("next", {}).get("url")
            params = None

    def graphql(self, query, variables):
        r = self.request("POST", "/graphql", json={"query": query, "variables": variables}).json()
        if r.get("errors"):
            raise RuntimeError(f"GraphQL errors: {r['errors']}")
        return r["data"]

    # -- source objects ------------------------------------------------------------
    def owner(self):
        kind = "orgs" if self.cfg.owner_type == "org" else "users"
        return self.get(f"/{kind}/{self.cfg.owner}")

    def repos(self):
        if self.cfg.app_id:
            items = self.paginate("/installation/repositories")
            return [r for r in items if r["owner"]["login"].lower() == self.cfg.owner.lower()]
        if self.cfg.owner_type == "org":
            return list(self.paginate(f"/orgs/{self.cfg.owner}/repos", {"type": "all"}))
        me = self.get("/user")["login"]
        if me.lower() == self.cfg.owner.lower():   # include the owner's private repos
            return [r for r in self.paginate("/user/repos", {"affiliation": "owner", "visibility": "all"})]
        return list(self.paginate(f"/users/{self.cfg.owner}/repos", {"type": "owner"}))

    def repo(self, full):
        return self.get(f"/repos/{full}")

    def issues_since(self, full, since=None):
        params = {"state": "all", "sort": "updated", "direction": "asc"}
        if since:
            params["since"] = since
        return list(self.paginate(f"/repos/{full}/issues", params))

    def issue(self, full, number):
        return self.get(f"/repos/{full}/issues/{number}")

    def pull(self, full, number):
        return self.get(f"/repos/{full}/pulls/{number}")

    def issue_comments(self, full, number):
        return list(self.paginate(f"/repos/{full}/issues/{number}/comments"))

    def reviews(self, full, number):
        return list(self.paginate(f"/repos/{full}/pulls/{number}/reviews"))

    def review_comments(self, full, number):
        return list(self.paginate(f"/repos/{full}/pulls/{number}/comments"))

    def reactions(self, url):
        return list(self.paginate(url))

    def labels(self, full):
        return list(self.paginate(f"/repos/{full}/labels"))

    def milestones(self, full):
        return list(self.paginate(f"/repos/{full}/milestones", {"state": "all"}))

    def releases(self, full):
        return list(self.paginate(f"/repos/{full}/releases"))

    def download(self, url, dest, accept="application/octet-stream"):
        r = self.request("GET", url, accept=accept, stream=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        return dest

    def user(self, login):
        return self.get(f"/users/{login}")

    def user_keys(self, login):
        return list(self.paginate(f"/users/{login}/keys"))

    def user_gpg_keys(self, login):
        return list(self.paginate(f"/users/{login}/gpg_keys"))

    def collaborators(self, full):
        try:
            return list(self.paginate(f"/repos/{full}/collaborators", {"affiliation": "direct"}))
        except (NotFound, RuntimeError):
            return []

    def branch_protections(self, full):
        out = {}
        try:
            branches = list(self.paginate(f"/repos/{full}/branches", {"protected": "true"}))
        except NotFound:
            return out
        for b in branches:
            try:
                out[b["name"]] = self.get(f"/repos/{full}/branches/{b['name']}/protection")
            except (NotFound, RuntimeError):
                out[b["name"]] = {"protected": True, "detail": "unreadable"}
        return out

    def rulesets(self, full):
        try:
            return list(self.paginate(f"/repos/{full}/rulesets"))
        except (NotFound, RuntimeError):
            return []

    def review_threads(self, owner, name, number):
        q = """query($o:String!,$r:String!,$n:Int!,$c:String){repository(owner:$o,name:$r){pullRequest(number:$n){
               reviewThreads(first:100,after:$c){pageInfo{hasNextPage endCursor}
               nodes{isResolved resolvedBy{login} comments(first:1){nodes{databaseId}}}}}}}"""
        out, cursor = [], None
        while True:
            d = self.graphql(q, {"o": owner, "r": name, "n": number, "c": cursor})
            pr = (d.get("repository") or {}).get("pullRequest")
            if not pr:
                return out
            rt = pr["reviewThreads"]
            for n in rt["nodes"]:
                if n["comments"]["nodes"]:
                    out.append({"comment_id": n["comments"]["nodes"][0]["databaseId"], "resolved": n["isResolved"],
                                "resolved_by": (n.get("resolvedBy") or {}).get("login")})
            if not rt["pageInfo"]["hasNextPage"]:
                return out
            cursor = rt["pageInfo"]["endCursor"]

    # org structure
    def org_members(self):
        out = {}
        for role in ("admin", "member"):
            for m in self.paginate(f"/orgs/{self.cfg.owner}/members", {"role": role}):
                out[m["login"]] = {"user": m, "role": role}
        return out

    def teams(self):
        return list(self.paginate(f"/orgs/{self.cfg.owner}/teams"))

    def team_members(self, slug):
        out = {}
        for role in ("maintainer", "member"):
            for m in self.paginate(f"/orgs/{self.cfg.owner}/teams/{slug}/members", {"role": role}):
                out[m["login"]] = {"user": m, "role": role}
        return out

    def team_repos(self, slug):
        return list(self.paginate(f"/orgs/{self.cfg.owner}/teams/{slug}/repos"))

    def outside_collaborators(self):
        try:
            return list(self.paginate(f"/orgs/{self.cfg.owner}/outside_collaborators"))
        except (NotFound, RuntimeError):
            return []

    # migration archive
    def start_migration(self, repos):
        body = {"repositories": repos, "lock_repositories": False, "exclude_attachments": False}
        # the migrations API refuses App and OAuth tokens: use the dedicated classic PAT when configured
        tok = self.cfg.migration_token
        if self.cfg.owner_type == "org":
            return self.request("POST", f"/orgs/{self.cfg.owner}/migrations", json=body, token=tok).json()
        return self.request("POST", "/user/migrations", json=body, token=tok).json()

    def migration(self, mid):
        base = f"/orgs/{self.cfg.owner}" if self.cfg.owner_type == "org" else "/user"
        return self.request("GET", f"{base}/migrations/{mid}", token=self.cfg.migration_token).json()

    def migration_archive_url(self, mid):
        base = f"/orgs/{self.cfg.owner}" if self.cfg.owner_type == "org" else "/user"
        r = self.request("GET", f"{base}/migrations/{mid}/archive", allow_redirects=False,
                         token=self.cfg.migration_token)
        return r.headers.get("Location")
