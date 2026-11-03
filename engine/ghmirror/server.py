"""Long-running service: webhook receiver + metrics, queue workers, scheduler.

Webhooks only accelerate; the scheduler's periodic passes are the guarantee of completeness.
"""
import hashlib
import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import Halted
from .github import NotFound
from .migration import run_migration
from .org import sync_org
from .verify import verify

log = logging.getLogger("ghmirror.server")

META_EVENTS = {"issues", "issue_comment", "pull_request", "pull_request_review", "pull_request_review_comment",
               "pull_request_review_thread", "label", "milestone", "release", "commit_comment"}
GIT_EVENTS = {"push", "create", "delete"}
ORG_EVENTS = {"member", "membership", "organization", "team", "team_add"}


MAX_BODY = 26 * 1024 * 1024   # GitHub caps webhook payloads at 25 MB


def verify_signature(secret, body, header):
    if not secret:
        return False   # fail closed: without a shared secret anyone could queue work
    if not header or not header.startswith("sha256="):
        return False
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header[len("sha256="):])


def route_event(db, event, payload):
    """Translate one GitHub delivery into coalesced jobs. Returns the job kinds enqueued."""
    repo = (payload.get("repository") or {}).get("full_name")
    jobs = []
    if event in GIT_EVENTS and repo:
        jobs.append(("git", repo, 0))
        if event == "push":
            jobs.append(("meta", repo, 5))   # merges arrive as a push plus a pull_request event
    elif event in META_EVENTS and repo:
        jobs.append(("meta", repo, 3))
    elif event == "repository":
        action = payload.get("action")
        if action in ("created", "unarchived", "publicized", "privatized", "edited", "renamed", "archived") and repo:
            jobs.append(("full", repo, 0))
        jobs.append(("repos", None, 0))
    elif event in ORG_EVENTS:
        jobs.append(("org", None, 10))
    elif event in ("installation", "installation_repositories"):
        jobs.append(("repos", None, 0))
    for kind, r, delay in jobs:
        db.enqueue(kind, r, delay=delay)
    return [j[0] for j in jobs]


class Service:
    def __init__(self, eng):
        self.e = eng
        self.stop = threading.Event()

    # ------------------------------------------------------------------ jobs
    def handle(self, job):
        e, kind, repo = self.e, job["kind"], job["repo"]
        if kind in ("git", "meta", "full", "repos", "org", "migration") and not e.source_active():
            return  # promoted: GitHub is no longer the source
        if kind == "repos":
            known = {r["gh_id"] for r in e.db.rows("SELECT DISTINCT gh_id FROM mapping WHERE kind='repo'")}
            for r in e.list_repos():
                if str(r["id"]) not in known:
                    e.db.enqueue("full", r["full_name"])
            return
        if kind in ("git", "meta", "full"):
            try:
                gh_repo = e.repo_by_name(repo)
            except NotFound:
                e.db.enqueue("repos")   # deleted or renamed: the guard-protected listing decides
                return
            if not e.selected(gh_repo):
                return
            e.sync_repo(gh_repo, git=kind in ("git", "full"), meta=kind in ("meta", "full"), full=kind == "full")
            return
        if kind == "org":
            sync_org(e)
        elif kind == "verify":
            verify(e, repair=True)
        elif kind == "migration":
            run_migration(e)

    def worker(self, n):
        while not self.stop.is_set():
            if self.e.guard.halted():
                self.stop.wait(15)
                continue
            job = self.e.db.claim()
            if not job:
                self.stop.wait(1.0)
                continue
            t0 = time.time()
            try:
                self.handle(job)
                self.e.db.finish(job)
                log.info("job %s %s done in %.1fs", job["kind"], job["repo"] or "", time.time() - t0)
            except Halted as err:
                self.e.db.finish(job, f"halted: {err}", retry_delay=300)
            except Exception as err:
                log.exception("job %s %s failed", job["kind"], job["repo"])
                self.e.db.finish(job, err)
                if job["attempts"] >= 5:
                    self.e.alert("error", f"job {job['kind']} {job['repo']} dead-lettered: {err}")

    def scheduler(self):
        sched = self.e.cfg.schedule
        while not self.stop.is_set():
            now = time.time()
            self.e.db.requeue_expired()
            for name, interval in sched.items():
                last = self.e.db.get(f"sched:{name}", 0)
                if now - last < interval:
                    continue
                self.e.db.set(f"sched:{name}", now)
                if not self.e.source_active() and name != "verify":
                    continue
                if name in ("git", "meta", "full"):
                    for r in self.e.db.rows("SELECT DISTINCT extra FROM mapping WHERE kind='repo'"):
                        x = json.loads(r["extra"] or "{}")
                        if x.get("full_name") and not x.get("deleted_on_source"):
                            self.e.db.enqueue(name, x["full_name"])
                else:
                    self.e.db.enqueue(name)
            self.stop.wait(5)

    # ------------------------------------------------------------------ http
    def metrics(self):
        db = self.e.db
        lines = []
        counts = db.job_counts()
        for st in ("pending", "running", "dead", "done"):
            lines.append(f'ghmirror_jobs{{status="{st}"}} {counts.get(st, 0)}')
        lines.append(f"ghmirror_halted {1 if self.e.guard.halted() else 0}")
        lv = db.get("last_verify") or {}
        lines.append(f"ghmirror_drift_count {lv.get('drift_count', -1)}")
        now = time.time()
        for r in db.rows("SELECT k, v FROM kv WHERE k LIKE 'lastmeta:%'"):
            _, target, repo_id = r["k"].split(":", 2)
            lines.append(f'ghmirror_meta_age_seconds{{target="{target}",repo_id="{repo_id}"}} '
                         f'{now - json.loads(r["v"]):.0f}')
        lines.append(f'ghmirror_alerts_total {db.row("SELECT COUNT(*) AS n FROM alerts")["n"]}')
        return "\n".join(lines) + "\n"

    def make_handler(self):
        svc = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug(fmt, *args)

            def _send(self, code, body, ctype="application/json"):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/healthz":
                    return self._send(200, json.dumps({"ok": True, "halted": svc.e.guard.halted()}))
                if self.path == "/metrics":
                    return self._send(200, svc.metrics(), "text/plain; version=0.0.4")
                if self.path == "/status":
                    return self._send(200, json.dumps(status(svc.e), default=str))
                self._send(404, '{"error":"not found"}')

            def do_POST(self):
                if self.path != "/webhook":
                    return self._send(404, '{"error":"not found"}')
                size = int(self.headers.get("Content-Length") or 0)
                if size > MAX_BODY:
                    return self._send(413, '{"error":"payload too large"}')
                body = self.rfile.read(size)
                if not verify_signature(svc.e.cfg.webhook_secret, body, self.headers.get("X-Hub-Signature-256")):
                    return self._send(401, '{"error":"bad signature"}')
                delivery = self.headers.get("X-GitHub-Delivery") or ""
                if delivery and svc.e.db.seen_delivery(delivery):
                    return self._send(200, '{"duplicate":true}')
                event = self.headers.get("X-GitHub-Event") or ""
                try:
                    payload = json.loads(body or b"{}")
                except ValueError:
                    return self._send(400, '{"error":"bad json"}')
                kinds = route_event(svc.e.db, event, payload)
                self._send(202, json.dumps({"queued": kinds}))

        return Handler

    def serve(self):
        host, port = self.e.cfg.listen.rsplit(":", 1)
        httpd = ThreadingHTTPServer((host, int(port)), self.make_handler())
        threads = [threading.Thread(target=self.worker, args=(i,), daemon=True) for i in range(self.e.cfg.workers)]
        threads.append(threading.Thread(target=self.scheduler, daemon=True))
        for t in threads:
            t.start()
        log.info("listening on %s with %d workers", self.e.cfg.listen, self.e.cfg.workers)
        try:
            httpd.serve_forever()
        finally:
            self.stop.set()


def status(eng):
    db = eng.db
    return {"source": db.get("source", "github"), "halted": eng.guard.halted(),
            "modes": {c.name: c.mode for c in eng.ctxs}, "jobs": db.job_counts(),
            "last_verify": db.get("last_verify"), "promotion": db.get("promotion"),
            "target_errors": {c.name: db.get(f"target_error:{c.name}") for c in eng.ctxs},
            "recent_alerts": db.rows("SELECT ts, level, message FROM alerts ORDER BY id DESC LIMIT 10")}
