import hashlib
import hmac
import os
import threading

import pytest

from ghmirror.db import DB
from ghmirror.server import route_event, verify_signature


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path):
    if request.param == "postgres":
        url = os.environ.get("GHMIRROR_TEST_PG")
        if not url:
            pytest.skip("set GHMIRROR_TEST_PG to run against PostgreSQL")
        d = DB(url)
        d.init()
        for t in ("mapping", "kv", "jobs", "deliveries", "audit", "alerts"):
            d.execute(f"DELETE FROM {t}")
        return d
    d = DB(f"sqlite:///{tmp_path}/t.db")
    d.init()
    return d


def test_mapping_roundtrip(db):
    db.map_put("cloud", "issue", 1, 10, 3, parent="r", extra={"a": 1}, hash="h")
    m = db.map_get("cloud", "issue", "1")
    assert m["gl_id"] == "10" and m["gl_iid"] == 3 and m["extra"] == {"a": 1}
    db.map_update("cloud", "issue", 1, hash="h2")
    assert db.map_get("cloud", "issue", 1)["hash"] == "h2"
    assert list(db.map_children("cloud", "issue", "r")) == ["1"]
    db.map_delete("cloud", "issue", 1)
    assert db.map_get("cloud", "issue", 1) is None


def test_queue_coalesce_and_repo_ordering(db):
    db.enqueue("meta", "o/a")
    db.enqueue("meta", "o/a")            # coalesced
    db.enqueue("git", "o/a")
    db.enqueue("meta", "o/b")
    assert db.job_counts()["pending"] == 3
    j1 = db.claim()
    assert j1["repo"] == "o/a"
    j2 = db.claim()
    assert j2["repo"] == "o/b"           # o/a is busy, so its second job waits
    assert db.claim() is None
    db.finish(j1)
    j3 = db.claim()
    assert j3["repo"] == "o/a" and j3["kind"] == "git"
    db.finish(j3, "boom", retry_delay=0)
    j4 = db.claim()
    assert j4["id"] == j3["id"] and j4["attempts"] == 2


def test_queue_concurrent_claims_are_exclusive(db):
    for i in range(30):
        db.enqueue("meta", f"o/r{i}")
    got = []
    lock = threading.Lock()

    def worker():
        while True:
            j = db.claim()
            if not j:
                return
            with lock:
                got.append(j["id"])
            db.finish(j)

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(got) == 30 and len(set(got)) == 30


def test_dead_letter(db):
    db.enqueue("meta", "o/x")
    for _ in range(5):
        j = db.claim()
        db.finish(j, "fail", retry_delay=0)
    assert db.job_counts().get("dead") == 1


def test_deliveries_and_kv(db):
    assert db.seen_delivery("d1") is False
    assert db.seen_delivery("d1") is True
    db.set("x", {"y": 1})
    assert db.get("x") == {"y": 1}


def test_signature():
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_signature("s3cret", body, sig)
    assert not verify_signature("s3cret", body, "sha256=00")
    assert not verify_signature("s3cret", body, None)
    assert not verify_signature("", body, sig)   # no secret configured: refuse, never accept unsigned


def test_routing(db):
    rp = {"repository": {"full_name": "o/a"}}
    assert route_event(db, "push", rp) == ["git", "meta"]
    assert route_event(db, "pull_request_review_comment", rp) == ["meta"]
    assert route_event(db, "repository", dict(rp, action="created")) == ["full", "repos"]
    assert route_event(db, "team", {}) == ["org"]
    assert route_event(db, "ping", {}) == []
