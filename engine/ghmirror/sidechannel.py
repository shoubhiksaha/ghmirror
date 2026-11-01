"""Side channel: a persistent Rails agent for the few fields GitLab's API cannot set.

`side_channel` in the target config is the command prefix that runs a program on the GitLab host:
    ["docker", "exec", "-i", "gitlab"]            # GitLab in a container
    ["ssh", "gitlab.internal", "sudo"]           # Omnibus on a VM
The engine installs sidechannel.rb on the host once per process, starts one long-lived
`gitlab-rails runner` with it (Rails boots once), then streams batches of operations as JSON
lines and reads one result per operation. Every operation is idempotent and audited.

Killing the local `docker exec` / `ssh` client does not stop the process on the GitLab host, so
each agent carries a unique token and is killed there with `pkill -9 -f <token>` (a booting
Rails ignores SIGTERM); the agent also exits by itself after `idle` seconds without input.
"""
import atexit
import json
import logging
import os
import shlex
import subprocess
import threading
import uuid
from collections import deque

log = logging.getLogger("ghmirror.sidechannel")
SCRIPT = os.path.join(os.path.dirname(__file__), "sidechannel.rb")
REMOTE_PATH = "/tmp/ghmirror-agent.rb"
MARK, READY, END = "GHMIRROR_RESULT ", "GHMIRROR_READY", "GHMIRROR_BATCH_END"


class SideChannelError(Exception):
    pass


class SideChannel:
    def __init__(self, prefix, db=None, target=None, boot_timeout=2700, idle=900):
        self.prefix = list(prefix or [])
        self.db = db
        self.target = target
        self.boot_timeout = boot_timeout
        self.idle = idle
        self.proc = None
        self.token = None
        self.stderr = deque(maxlen=20)
        self.lock = threading.Lock()
        self.closing = False
        if self.prefix:
            atexit.register(self._at_exit)

    @property
    def enabled(self):
        return bool(self.prefix)

    def _cmd(self, args):
        # ssh joins its arguments into one string for the remote shell: quote them, or a redirect
        # such as "cat > file" would run in the login shell (outside a GitLab container, say)
        if os.path.basename(self.prefix[0]) == "ssh":
            return self.prefix + [shlex.join(args)]
        return self.prefix + args

    def _start(self):
        with open(SCRIPT) as f:
            script = f.read()
        p = subprocess.run(self._cmd(["sh", "-c", f"cat > {REMOTE_PATH}"]), input=script, text=True,
                           capture_output=True, timeout=120)
        if p.returncode != 0:
            raise SideChannelError(f"installing agent failed: {p.stderr[-500:]}")
        self.token = f"ghmirror-agent-{uuid.uuid4().hex[:12]}"
        self.stderr.clear()
        self.proc = subprocess.Popen(self._cmd(["gitlab-rails", "runner", REMOTE_PATH, self.token, str(self.idle)]),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, bufsize=1)
        threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
        expired = threading.Event()

        def expire():
            expired.set()
            self._kill()
        timer = threading.Timer(self.boot_timeout, expire)
        timer.start()
        try:
            for line in self.proc.stdout:
                if line.strip() == READY:
                    log.info("[%s] side-channel agent ready", self.target)
                    return
            if expired.is_set():
                raise SideChannelError(f"agent not ready after {self.boot_timeout}s (GitLab host overloaded?); killed")
            raise SideChannelError(f"agent exited before becoming ready: {' | '.join(self.stderr)[-500:]}")
        finally:
            timer.cancel()

    def _drain_stderr(self, proc):
        for line in proc.stderr:
            self.stderr.append(line.rstrip())

    def _kill(self):
        """Stop the agent here and on the GitLab host."""
        proc, token = self.proc, self.token
        if proc and proc.poll() is None:
            proc.kill()
        if token:
            try:
                # "[g]hmirror-agent-…" matches the agent but not this command line (nor sudo / sh carrying it)
                subprocess.run(self._cmd(["pkill", "-9", "-f", f"[{token[0]}]{token[1:]}"]), capture_output=True, timeout=60)
            except (subprocess.SubprocessError, OSError) as e:
                log.warning("[%s] could not stop agent %s on the GitLab host: %s", self.target, token, e)

    def warm(self):
        """Boot the agent in the background so Rails is ready by the time the first batch is flushed."""
        if not self.enabled or (self.proc and self.proc.poll() is None):
            return

        def boot():
            with self.lock:
                if self.proc is None or self.proc.poll() is not None:
                    try:
                        self._start()
                    except Exception as e:
                        if not self.closing:   # killed on purpose at process exit: not a failure
                            log.warning("[%s] agent warm-up failed: %s", self.target, e)
                        self._kill()
                        self.proc = None
        threading.Thread(target=boot, daemon=True).start()

    def _at_exit(self):
        self.closing = True
        self.close()

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=30)
            except Exception:
                pass
        self._kill()   # also covers an agent still booting, which has not read its stdin yet
        self.proc = self.token = None

    def run(self, ops):
        """Execute a batch; returns [{i, ok, error?, op}] in order."""
        if not ops:
            return []
        if not self.enabled:
            return [{"i": i, "ok": False, "error": "side channel not configured", "op": op} for i, op in enumerate(ops)]
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.proc is None or self.proc.poll() is not None:
                        self._start()
                    self.proc.stdin.write(json.dumps(ops) + "\n")
                    self.proc.stdin.flush()
                    results = {}
                    for line in self.proc.stdout:
                        line = line.rstrip("\n")
                        if line == END:
                            break
                        if line.startswith(MARK):
                            r = json.loads(line[len(MARK):])
                            results[r["i"]] = r
                    else:
                        raise SideChannelError("agent exited mid-batch")
                    break
                except (BrokenPipeError, SideChannelError, OSError) as e:
                    self.close()
                    if attempt == 2:
                        raise SideChannelError(str(e))
                    log.warning("[%s] side-channel agent restart: %s", self.target, e)
        out = []
        for i, op in enumerate(ops):
            r = results.get(i, {"i": i, "ok": False, "error": "no result"})
            r["op"] = op
            out.append(r)
            if self.db:
                self.db.audit(self.target, "sidechannel." + op["op"],
                              {"op": op, "ok": r["ok"], "error": r.get("error")})
            if not r["ok"]:
                log.error("[%s] side-channel op failed: %s %s", self.target, op, r.get("error"))
        return out
