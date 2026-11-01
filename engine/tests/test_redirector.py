from ghmirror.redirector import rewrite

B, G = "https://git.corp.com", "acme"


def r(p):
    return rewrite(p, "acme", B, G)


def test_routes():
    assert r("/acme/app/pull/12") == "https://git.corp.com/acme/app/-/merge_requests/12"
    assert r("/acme/app/pull/12/files") == "https://git.corp.com/acme/app/-/merge_requests/12/diffs"
    assert r("/acme/app/issues/7") == "https://git.corp.com/acme/app/-/issues/7"
    assert r("/acme/app/blob/main/src/x.py") == "https://git.corp.com/acme/app/-/blob/main/src/x.py"
    assert r("/acme/app/commit/abc") == "https://git.corp.com/acme/app/-/commit/abc"
    assert r("/acme/app/releases/tag/v1.0") == "https://git.corp.com/acme/app/-/releases/v1.0"
    assert r("/acme/app/wiki") == "https://git.corp.com/acme/app/-/wikis/home"
    assert r("/acme/app.git") == "https://git.corp.com/acme/app"
    assert r("/acme/app?tab=readme") == "https://git.corp.com/acme/app?tab=readme"
    assert r("/acme") == "https://git.corp.com/acme"
    assert r("/other/app/pull/1") is None
