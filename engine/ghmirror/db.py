"""State store: id mappings, key/value state, durable job queue, webhook dedupe, audit, alerts.

Works on SQLite (single host, small orgs, tests) and PostgreSQL (production). SQL is written
with '?' placeholders and translated for psycopg.
"""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS mapping (
    target TEXT NOT NULL, kind TEXT NOT NULL, gh_id TEXT NOT NULL,
    gl_id TEXT, gl_iid INTEGER, parent TEXT, extra TEXT, hash TEXT, gh_updated TEXT, synced_at DOUBLE PRECISION,
    PRIMARY KEY (target, kind, gh_id));
CREATE INDEX IF NOT EXISTS mapping_parent ON mapping (target, kind, parent);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS jobs (
    id {serial}, key TEXT NOT NULL, kind TEXT NOT NULL, repo TEXT, payload TEXT,
    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    run_after DOUBLE PRECISION NOT NULL, locked_until DOUBLE PRECISION, last_error TEXT,
    created DOUBLE PRECISION NOT NULL, finished DOUBLE PRECISION);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_pending_key ON jobs (key) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, run_after);
CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, received DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS audit (id {serial}, ts DOUBLE PRECISION NOT NULL, target TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS alerts (id {serial}, ts DOUBLE PRECISION NOT NULL, level TEXT, message TEXT);
"""

MAX_ATTEMPTS = 5


class DB:
    def __init__(self, url):
        self.url = url
        self.pg = url.startswith(("postgres://", "postgresql://"))
        self._local = threading.local()
        if not self.pg and not url.startswith("sqlite:///"):
            raise ValueError("database must be sqlite:///path or postgresql://...")

    # -- connection handling -------------------------------------------------
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            if self.pg:
                import psycopg
                c = psycopg.connect(self.url, autocommit=True)
            else:
                c = sqlite3.connect(self.url[len("sqlite:///"):], timeout=60, isolation_level=None,
                                    check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA busy_timeout=60000")
            self._local.conn = c
        return c

    def execute(self, sql, params=()):
        if self.pg:
            sql = sql.replace("?", "%s")
        cur = self.conn().cursor()
        cur.execute(sql, params)
        return cur

    def rows(self, sql, params=()):
        cur = self.execute(sql, params)
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def row(self, sql, params=()):
        r = self.rows(sql, params)
        return r[0] if r else None

    def init(self):
        serial = "BIGSERIAL PRIMARY KEY" if self.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        for stmt in SCHEMA.format(serial=serial).split(";"):
            if stmt.strip():
                self.execute(stmt)

    # -- id mapping ------------------------------------------------------------
    def map_get(self, target, kind, gh_id):
        r = self.row("SELECT * FROM mapping WHERE target=? AND kind=? AND gh_id=?", (target, kind, str(gh_id)))
        if r and r.get("extra"):
            r["extra"] = json.loads(r["extra"])
        elif r:
            r["extra"] = {}
        return r

    def map_put(self, target, kind, gh_id, gl_id=None, gl_iid=None, parent=None, extra=None, hash=None,
                gh_updated=None):
        self.execute(
            "INSERT INTO mapping (target, kind, gh_id, gl_id, gl_iid, parent, extra, hash, gh_updated, synced_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (target, kind, gh_id) DO UPDATE SET "
            "gl_id=excluded.gl_id, gl_iid=excluded.gl_iid, parent=excluded.parent, extra=excluded.extra, "
            "hash=excluded.hash, gh_updated=excluded.gh_updated, synced_at=excluded.synced_at",
            (target, kind, str(gh_id), None if gl_id is None else str(gl_id), gl_iid, parent,
             json.dumps(extra or {}), hash, gh_updated, time.time()))

    def map_update(self, target, kind, gh_id, **fields):
        r = self.map_get(target, kind, gh_id)
        if not r:
            return
        r.update(fields)
        self.map_put(target, kind, gh_id, r["gl_id"], r["gl_iid"], r["parent"], r["extra"], r["hash"],
                     r["gh_updated"])

    def map_children(self, target, kind, parent):
        out = {}
        for r in self.rows("SELECT * FROM mapping WHERE target=? AND kind=? AND parent=?", (target, kind, parent)):
            r["extra"] = json.loads(r["extra"]) if r.get("extra") else {}
            out[r["gh_id"]] = r
        return out

    def map_all(self, target, kind):
        rows = self.rows("SELECT * FROM mapping WHERE target=? AND kind=?", (target, kind))
        for r in rows:
            r["extra"] = json.loads(r["extra"]) if r.get("extra") else {}
        return rows

    def map_delete(self, target, kind, gh_id):
        self.execute("DELETE FROM mapping WHERE target=? AND kind=? AND gh_id=?", (target, kind, str(gh_id)))

    # -- key/value -------------------------------------------------------------
    def get(self, k, default=None):
        r = self.row("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def set(self, k, v):
        self.execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v=excluded.v",
                     (k, json.dumps(v)))

    def delete(self, k):
        self.execute("DELETE FROM kv WHERE k=?", (k,))

    # -- job queue ---------------------------------------------------------------
    def enqueue(self, kind, repo=None, payload=None, delay=0.0, key=None):
        """Insert a pending job. A pending job with the same key absorbs the new one (coalescing)."""
        key = key or f"{kind}:{repo or ''}"
        now = time.time()
        self.execute("INSERT INTO jobs (key, kind, repo, payload, status, attempts, run_after, created) "
                     "VALUES (?,?,?,?, 'pending', 0, ?, ?) ON CONFLICT (key) WHERE status = 'pending' DO NOTHING",
                     (key, kind, repo, json.dumps(payload or {}), now + delay, now))

    def claim(self, lease=1800):
        """Take the oldest runnable job whose repository has no other running job (per-repo ordering)."""
        now = time.time()
        busy = "SELECT repo FROM jobs WHERE status='running' AND locked_until > ? AND repo IS NOT NULL"
        pick = (f"SELECT id FROM jobs WHERE status='pending' AND run_after <= ? "
                f"AND (repo IS NULL OR repo NOT IN ({busy})) ORDER BY run_after, id LIMIT 1")
        if self.pg:
            pick += " FOR UPDATE SKIP LOCKED"
            with self.conn().transaction():
                r = self.row(f"UPDATE jobs SET status='running', locked_until=?, attempts=attempts+1 "
                             f"WHERE id = ({pick}) RETURNING *", (now + lease, now, now))
        else:
            self.execute("BEGIN IMMEDIATE")
            try:
                r = self.row(f"UPDATE jobs SET status='running', locked_until=?, attempts=attempts+1 "
                             f"WHERE id = ({pick}) RETURNING *", (now + lease, now, now))
                self.execute("COMMIT")
            except Exception:
                self.execute("ROLLBACK")
                raise
        if r:
            r["payload"] = json.loads(r["payload"] or "{}")
        return r

    def finish(self, job, error=None, retry_delay=None):
        now = time.time()
        if error is None:
            self.execute("UPDATE jobs SET status='done', finished=?, last_error=NULL WHERE id=?", (now, job["id"]))
        elif job["attempts"] >= MAX_ATTEMPTS:
            self.execute("UPDATE jobs SET status='dead', finished=?, last_error=? WHERE id=?",
                         (now, str(error)[:4000], job["id"]))
        else:
            delay = retry_delay if retry_delay is not None else min(3600, 30 * 2 ** job["attempts"])
            # back to pending unless an identical job is already pending (then this one is redundant)
            dup = self.row("SELECT id FROM jobs WHERE key=? AND status='pending'", (job["key"],))
            if dup:
                self.execute("UPDATE jobs SET status='done', finished=?, last_error=? WHERE id=?",
                             (now, str(error)[:4000], job["id"]))
            else:
                self.execute("UPDATE jobs SET status='pending', run_after=?, last_error=? WHERE id=?",
                             (now + delay, str(error)[:4000], job["id"]))

    def requeue_expired(self):
        self.execute("UPDATE jobs SET status='pending' WHERE status='running' AND locked_until < ? "
                     "AND key NOT IN (SELECT key FROM jobs WHERE status='pending')", (time.time(),))

    def job_counts(self):
        return {r["status"]: r["n"] for r in self.rows("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}

    # -- webhook dedupe, audit, alerts -----------------------------------------------
    def seen_delivery(self, delivery_id):
        """Record a webhook delivery id; True if it was already recorded."""
        try:
            self.execute("INSERT INTO deliveries (id, received) VALUES (?, ?)", (delivery_id, time.time()))
            return False
        except Exception as e:  # unique violation
            if "UNIQUE" in str(e).upper() or "duplicate" in str(e).lower():
                return True
            raise

    def audit(self, target, action, detail):
        self.execute("INSERT INTO audit (ts, target, action, detail) VALUES (?,?,?,?)",
                     (time.time(), target, action, json.dumps(detail, default=str)[:8000]))

    def add_alert(self, level, message):
        self.execute("INSERT INTO alerts (ts, level, message) VALUES (?,?,?)", (time.time(), level, message))
