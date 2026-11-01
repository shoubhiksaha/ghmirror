"""GitLab target client: REST v4 with admin token, Sudo impersonation and retries."""
import base64
import logging
import time
from urllib.parse import quote

import requests

log = logging.getLogger("ghmirror.gitlab")

OWNER, MAINTAINER, DEVELOPER, REPORTER, GUEST = 50, 40, 30, 20, 10


class GLNotFound(Exception):
    pass


class GLError(Exception):
    def __init__(self, status, text, method="", path=""):
        super().__init__(f"GitLab {method} {path}: {status} {text[:500]}")
        self.status = status
        self.text = text


class TargetUnavailable(Exception):
    pass


def enc(path):
    return quote(str(path), safe="")


class GitLab:
    def __init__(self, target):
        self.t = target
        self.name = target.name
        self.api = target.api
        self.s = requests.Session()
        self.s.headers.update({"PRIVATE-TOKEN": target.token, "User-Agent": "ghmirror"})

    def git_url(self, path_with_namespace, wiki=False):
        base = (self.t.git_url or self.t.url).rstrip("/")
        return f"{base}/{path_with_namespace}{'.wiki' if wiki else ''}.git"

    def git_env(self):
        cred = base64.b64encode(f"ghmirror:{self.t.token}".encode()).decode()
        base = (self.t.git_url or self.t.url).rstrip("/")
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{base}/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {cred}", "GIT_TERMINAL_PROMPT": "0"}

    def request(self, method, path, sudo=None, params=None, data=None, json=None, files=None, raw=False,
                ok404=False, recover=None):
        """recover: for a POST that creates something, a callable that looks the object up. A POST whose
        outcome is unknown (timeout, 500/502/504 from the proxy) may already have been applied, so before
        it is resent recover() is asked; if it finds the object, that is returned instead of a duplicate."""
        url = self.api + path
        headers = {}
        if sudo is not None:
            headers["Sudo"] = str(sudo)
        attempt = auth_waits = 0
        ambiguous = False
        while True:
            try:
                if hasattr(data, "seek"):
                    data.seek(0)
                r = self.s.request(method, url, params=params, data=data, json=json, files=files, headers=headers,
                                   timeout=180)
            except requests.RequestException as e:
                attempt += 1
                if attempt >= 6:
                    raise TargetUnavailable(f"{self.name}: {e}")
                time.sleep(min(30, 2 ** attempt))
                if not isinstance(e, requests.ConnectTimeout):   # anything later may have reached GitLab
                    ambiguous = True
                    found = self._recover(recover, method, path, e)
                    if found is not None:
                        return found
                continue
            if attempt < 8 and (r.status_code in (429, 502, 503, 504) or (r.status_code == 500 and attempt < 2)):
                attempt += 1   # overloaded / restarting GitLab: back off for up to ~5 minutes in total
                time.sleep(int(r.headers.get("Retry-After", 0)) or min(60, 2 ** attempt))
                if r.status_code in (500, 502, 504):   # 429/503 are refused before any work is done
                    ambiguous = True
                    found = self._recover(recover, method, path, r.status_code)
                    if found is not None:
                        return found
                continue
            if r.status_code == 404 and sudo is not None and "Project Not Found" in r.text and auth_waits < 20:
                # project_authorizations are refreshed asynchronously after a membership change or a
                # project creation, so an impersonated user can briefly not see the project yet
                auth_waits += 1
                time.sleep(min(30, 3 * auth_waits))
                continue
            if r.status_code == 404:
                if ok404:
                    return None
                raise GLNotFound(f"{self.name} {method} {path}: {r.text[:200]}")
            if ambiguous and r.status_code in (400, 409, 422):
                # a proxy timeout does not stop GitLab: the first attempt can finish after the lookup
                # missed it, and the resend then conflicts ("has already been taken", "Duplicated")
                for wait in (0, 3, 6, 12, 24):
                    time.sleep(wait)
                    found = self._recover(recover, method, path, f"{r.status_code} on resend")
                    if found is not None:
                        return found
            if r.status_code >= 400:
                raise GLError(r.status_code, r.text, method, path)
            if raw:
                return r
            return r.json() if r.content else None

    def _recover(self, recover, method, path, why):
        if not recover or method != "POST":
            return None
        try:
            found = recover()
        except (requests.RequestException, GLError, GLNotFound, TargetUnavailable) as e:
            log.info("[%s] POST %s: lookup after unknown outcome failed (%s); resending", self.name, path, e)
            return None
        if found is not None:
            log.info("[%s] POST %s: outcome unknown (%s) but the object exists; not resending", self.name, path, why)
        return found

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def put(self, path, **kw):
        return self.request("PUT", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def paginate(self, path, params=None, sudo=None):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page = 1
        while True:
            params["page"] = page
            r = self.request("GET", path, params=params, sudo=sudo, raw=True)
            items = r.json()
            yield from items
            nxt = r.headers.get("X-Next-Page")
            if not nxt:
                return
            page = int(nxt)

    # -- lookups --------------------------------------------------------------------
    def version(self):
        return self.get("/version")

    def group(self, full_path):
        return self.get(f"/groups/{enc(full_path)}", ok404=True)

    def project(self, full_path):
        return self.get(f"/projects/{enc(full_path)}", ok404=True)

    def user_by_username(self, username):
        users = self.get("/users", params={"username": username})
        return users[0] if users else None

    def mr(self, pid, iid):
        return self.get(f"/projects/{pid}/merge_requests/{iid}", ok404=True)

    def issue(self, pid, iid):
        return self.get(f"/projects/{pid}/issues/{iid}", ok404=True)

    def notes(self, pid, noun, iid):
        return list(self.paginate(f"/projects/{pid}/{noun}/{iid}/notes", {"sort": "asc", "order_by": "created_at"}))

    def discussions(self, pid, noun, iid):
        return list(self.paginate(f"/projects/{pid}/{noun}/{iid}/discussions"))
