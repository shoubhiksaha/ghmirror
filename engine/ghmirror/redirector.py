"""Rewrite old GitHub URLs to the GitLab that took over (bookmarks, docs, chat links keep working)."""
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RULES = [
    (r"^/(?P<o>[^/]+)/(?P<r>[^/]+)/pull/(?P<n>\d+)/files/?$", "{g}/{r}/-/merge_requests/{n}/diffs"),
    (r"^/(?P<o>[^/]+)/(?P<r>[^/]+)/pull/(?P<n>\d+)/commits/?$", "{g}/{r}/-/merge_requests/{n}/commits"),
    (r"^/(?P<o>[^/]+)/(?P<r>[^/]+)/pull/(?P<n>\d+)(?:/.*)?$", "{g}/{r}/-/merge_requests/{n}"),
    (r"^/(?P<o>[^/]+)/(?P<r>[^/]+)/pulls/?$", "{g}/{r}/-/merge_requests"),
    (r"^/(?P<o>[^/]+)/(?P<r>[^/]+)/issues/(?P<n>\d+)(?:/.*)?$", "{g}/{r}/-/issues/{n}"),
    (r"^/(?P<o>[^/]+)/(?P<r>[^/]+)/issues/?$", "{g}/{r}/-/issues"),
    (r"^/(?P<o>[^/]+)/(?P<r>[^/]+)/releases/tag/(?P<rest>.+)$", "{g}/{r}/-/releases/{rest}"),
    (r"^/(?P<o>[^/]+)/(?P<r>[^/]+)/releases/?$", "{g}/{r}/-/releases"),
    (r"^/(?P<o>[^/]+)/(?P<r>[^/]+)/(?P<kind>tree|blob|commit|commits|compare|tags|branches|raw|blame)/(?P<rest>.*)$",
     "{g}/{r}/-/{kind}/{rest}"),
    (r"^/(?P<o>[^/]+)/(?P<r>[^/]+)/wiki(?:/(?P<rest>.*))?$", "{g}/{r}/-/wikis/{rest_or_home}"),
    (r"^/(?P<o>[^/]+)/(?P<r>[^/.]+)(?:\.git)?/?$", "{g}/{r}"),
    (r"^/(?P<o>[^/]+)/?$", "{g}"),
]


def rewrite(path, owner, base_url, group):
    """Map a github.com path for `owner` to the GitLab URL; None if it is not ours."""
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
        query = "?" + query
    for pat, tmpl in RULES:
        m = re.match(pat, path)
        if not m:
            continue
        gd = m.groupdict()
        if gd["o"].lower() != owner.lower():
            return None
        r = gd.get("r")
        rest = gd.get("rest")
        if gd.get("kind") == "commits" and rest == "":
            rest = ""
        url = tmpl.format(g=f"{base_url.rstrip('/')}/{group}", r=r, n=gd.get("n"), kind=gd.get("kind"),
                          rest=rest or "", rest_or_home=rest or "home")
        return url + query
    return None


def serve(eng, listen):
    cfg = eng.cfg
    target = cfg.target(cfg.redirect_target or cfg.targets[0].name)
    base = cfg.redirect_base_url or target.url

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            url = rewrite(self.path, cfg.owner, base, target.group)
            if url:
                self.send_response(301)
                self.send_header("Location", url)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    host, port = listen.rsplit(":", 1)
    ThreadingHTTPServer((host, int(port)), H).serve_forever()
