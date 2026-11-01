"""Command line: ghmirror -c config.yaml <command>."""
import argparse
import json
import logging
import signal
import sys

from . import config as config_mod


def main(argv=None):
    # docker stop / systemd send SIGTERM: exit through atexit so side-channel agents are stopped
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    ap = argparse.ArgumentParser(prog="ghmirror", description=__doc__)
    ap.add_argument("-c", "--config", default="ghmirror.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="guided setup: write ghmirror.yaml, .env, identities.yaml, secrets/")
    i.add_argument("--dir", default=".", help="where to write the files (default: current directory)")
    i.add_argument("--owner", help="GitHub organization or user to mirror")
    i.add_argument("--owner-type", choices=["org", "user"], help="default: detected")
    i.add_argument("--github-auth", choices=["app", "token"], help="create a GitHub App (default) or use a token")
    i.add_argument("--github-token", help="classic PAT (default: $GITHUB_TOKEN)")
    i.add_argument("--new-app", action="store_true", help="create a new GitHub App even if one is configured")
    i.add_argument("--webhook-url", help="public HTTPS base URL for GitHub webhooks; '' for none (polling)")
    i.add_argument("--target-names", nargs="+", metavar="NAME", help="GitLab target names (default: cloud offline)")
    i.add_argument("--target", action="append", metavar="NAME=LINE", help="the ghmirror-target line for NAME")
    i.add_argument("--group", help="top-level GitLab group (default: the GitHub owner's name)")
    i.add_argument("--include", nargs="+", metavar="GLOB", help="repositories to mirror (default: all)")
    i.add_argument("--alert-webhook", help="Slack-compatible alert URL; '' for none")
    i.add_argument("--callback-port", type=int, default=8765, help="local port for the GitHub App manifest flow")
    i.add_argument("--callback-bind", default="127.0.0.1", help="address that port listens on")
    i.add_argument("--yes", action="store_true", help="never prompt: fail on missing answers, keep existing targets")
    sub.add_parser("check", help="verify connectivity, permissions and side channels")
    s = sub.add_parser("sync", help="one-shot sync (seed or catch-up) without the queue")
    s.add_argument("--repo", action="append", help="repository name (repeatable); default all")
    s.add_argument("--full", action="store_true", help="full metadata pass (deletions, reactions)")
    s.add_argument("--no-git", action="store_true")
    s.add_argument("--no-meta", action="store_true")
    s.add_argument("--org", action="store_true", help="also sync members/teams")
    sub.add_parser("run", help="service: webhooks + workers + scheduler")
    v = sub.add_parser("verify", help="parity report GitHub vs targets")
    v.add_argument("--repo", action="append")
    v.add_argument("--repair", action="store_true")
    p = sub.add_parser("promote", help="fail over to a GitLab target")
    p.add_argument("--primary", required=True)
    p.add_argument("--yes", action="store_true")
    sub.add_parser("demote", help="undo promote (drills)")
    g = sub.add_parser("guard", help="deletion guard state")
    g.add_argument("action", choices=["status", "reset"])
    sub.add_parser("status")
    m = sub.add_parser("archive-migration", help="GitHub migration archive into the L2 store")
    m.add_argument("--no-wait", action="store_true")
    r = sub.add_parser("redirector", help="serve old GitHub URLs as redirects to GitLab")
    r.add_argument("--listen", default="0.0.0.0:8090")
    sub.add_parser("enqueue-all", help="queue a full pass for every repository")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if args.cmd == "init":   # runs before a config exists
        from .setup import SetupError, init
        try:
            sys.exit(init(args))
        except SetupError as e:
            print(f"setup stopped: {e}", file=sys.stderr)
            sys.exit(2)
    cfg = config_mod.load(args.config)
    from .engine import Engine, Halted
    eng = Engine(cfg)

    if args.cmd == "check":
        ok = True
        o = eng.gh.owner()
        print(f"github: {o.get('login')} ({cfg.owner_type}), repos visible: {len([r for r in eng.gh.repos() if eng.selected(r)])}")
        eng.prepare()
        for c in eng.ctxs:
            if not c.group_id:
                print(f"target {c.name}: UNAVAILABLE {eng.db.get(f'target_error:{c.name}')}")
                ok = False
                continue
            ver = c.gl.version()["version"]
            sc = "none"
            if c.sc.enabled:
                try:
                    res = c.sc.run([{"op": "ping"}])
                    sc = f"ok (Rails {res[0].get('version')})" if res[0]["ok"] else f"FAILED {res[0].get('error')}"
                    ok = ok and res[0]["ok"]
                except Exception as e:
                    sc = f"FAILED {e}"
                    ok = False
            print(f"target {c.name}: GitLab {ver}, group {c.t.group} (id {c.group_id}), bot {c.bot_username}, "
                  f"mode {c.mode}, side channel {sc}")
        sys.exit(0 if ok else 1)

    if args.cmd == "sync":
        try:
            repos = eng.list_repos()
        except Halted as e:
            print(f"HALTED: {e}")
            sys.exit(3)
        if args.repo:
            repos = [r for r in repos if r["name"] in args.repo or r["full_name"] in args.repo]
        failed = 0
        for repo in repos:
            try:
                eng.sync_repo(repo, git=not args.no_git, meta=not args.no_meta, full=args.full)
                print(f"synced {repo['full_name']}")
            except Halted as e:
                print(f"HALTED: {e}")
                sys.exit(3)
            except Exception as e:
                failed += 1
                logging.exception("sync of %s failed", repo["full_name"])
                print(f"FAILED {repo['full_name']}: {e}")
        if args.org:
            from .org import sync_org
            sync_org(eng)
        sys.exit(1 if failed else 0)

    if args.cmd == "run":
        from .server import Service
        Service(eng).serve()

    if args.cmd == "verify":
        from .verify import verify
        rep = verify(eng, only=args.repo, repair=args.repair)
        print(json.dumps({k: rep[k] for k in ("checked", "drift_count")}))
        for d in rep["drift"][:50]:
            print("DRIFT", json.dumps(d))
        sys.exit(1 if rep["drift_count"] else 0)

    if args.cmd == "promote":
        if not args.yes:
            ans = input(f"Promote {args.primary} to PRIMARY and stop mirroring from GitHub? type 'promote': ")
            if ans.strip() != "promote":
                sys.exit(2)
        from .promote import promote
        rec = promote(eng, args.primary)
        print(json.dumps({k: rec[k] for k in ("primary", "state", "users", "mirrors", "rpo", "failures")}, default=str))
        sys.exit(1 if rec["failures"] else 0)

    if args.cmd == "demote":
        from .promote import demote
        demote(eng)
        print("demoted: MIRROR mode restored")

    if args.cmd == "guard":
        if args.action == "reset":
            eng.guard.reset()
        print(json.dumps({"halted": eng.guard.halted()}))

    if args.cmd == "status":
        from .server import status
        print(json.dumps(status(eng), indent=1, default=str))

    if args.cmd == "archive-migration":
        from .migration import run_migration
        print(json.dumps(run_migration(eng, wait=not args.no_wait)))

    if args.cmd == "redirector":
        from .redirector import serve
        serve(eng, args.listen)

    if args.cmd == "enqueue-all":
        for r in eng.list_repos():
            eng.db.enqueue("full", r["full_name"])
        print(json.dumps(eng.db.job_counts()))


if __name__ == "__main__":
    main()
