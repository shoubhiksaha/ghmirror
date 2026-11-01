"""Pure translation rules between GitHub and GitLab objects (unit-tested, no I/O)."""
import hashlib
import json
import re
from datetime import datetime, timezone

from .gitlab import DEVELOPER, GUEST, MAINTAINER, OWNER, REPORTER

DELETED_LABEL = "deleted-on-github"   # issue/MR kept on GitLab after it vanished from GitHub

EMOJI = {"+1": "thumbsup", "-1": "thumbsdown", "laugh": "laughing", "confused": "confused",
         "heart": "heart", "hooray": "tada", "rocket": "rocket", "eyes": "eyes"}

# GitHub repository permission / role name -> GitLab access level
ROLE = {"read": REPORTER, "pull": REPORTER, "triage": REPORTER, "write": DEVELOPER, "push": DEVELOPER,
        "maintain": MAINTAINER, "admin": OWNER, "none": None}

VISIBILITY_ORDER = ["private", "internal", "public"]

# Names GitLab will not accept as top-level usernames (subset; collisions are retried with a suffix anyway).
RESERVED_USERNAMES = {"admin", "ghost", "root", "support-bot", "alert-bot", "api", "help", "explore", "dashboard",
                      "groups", "projects", "users", "search", "-", "import", "s", "u", "assets", "profile",
                      "public", "snippets", "uploads", "users", "oauth", "sent_notifications", "jwt", "login"}


def username_for(login):
    """GitHub login -> GitLab username. 'dependabot[bot]' -> 'dependabot-bot'."""
    u = login.replace("[bot]", "-bot")
    u = re.sub(r"[^A-Za-z0-9_.-]", "-", u).strip("-.") or "user"
    if u.lower().endswith((".git", ".atom")):
        u += "-gh"
    if u.lower() in RESERVED_USERNAMES:
        u += "-gh"
    return u


def email_for(login, domain, email_map=None):
    if email_map and login in email_map:
        return email_map[login]
    local = re.sub(r"[^a-z0-9._-]", "-", login.lower().replace("[bot]", "-bot")).strip("-.") or "user"
    return f"{local}@{domain}"


def is_bot(user):
    return user.get("type") == "Bot" or user.get("login", "").endswith("[bot]")


def visibility(repo, cap):
    v = repo.get("visibility") or ("private" if repo.get("private") else "public")
    if v not in VISIBILITY_ORDER:
        v = "private"
    return VISIBILITY_ORDER[min(VISIBILITY_ORDER.index(v), VISIBILITY_ORDER.index(cap))]


def project_path(name):
    """GitHub repo name -> GitLab project path (GitLab forbids leading '-', '.git'/'.atom' suffixes)."""
    p = re.sub(r"[^A-Za-z0-9_.-]", "-", name)
    if p.startswith(("-", ".")):
        p = "_" + p[1:]
    if p.lower().endswith((".git", ".atom")):
        p += "-gh"
    return p


def iso(ts):
    """Normalize a timestamp to second precision UTC 'YYYY-MM-DDTHH:MM:SSZ' for comparisons."""
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:20]


def mr_title(pr):
    t = pr["title"]
    return f"Draft: {t}" if pr.get("draft") and not t.lower().startswith(("draft:", "[draft]", "(draft)")) else t


def label_color(c):
    c = (c or "ededed").lstrip("#")
    return "#" + c.lower()


def package_version(tag):
    v = re.sub(r"[^0-9A-Za-z.\-_+]", "_", tag)
    return v if v and v[0].isalnum() else "v" + v


def package_filename(name):
    return re.sub(r"[^0-9A-Za-z.\-_+~]", "_", name)


def pr_state(pr):
    if pr.get("merged_at"):
        return "merged"
    return "closed" if pr["state"] == "closed" else "opened"


def review_note_body(review):
    """Body of the MR note that represents a GitHub review (None = no note needed)."""
    body = (review.get("body") or "").strip()
    state = review.get("state")
    if state == "CHANGES_REQUESTED":
        return ":warning: **Requested changes**" + (f"\n\n{review['body']}" if body else "")
    if state == "DISMISSED" and not body:
        return None
    if state == "PENDING":
        return None
    return review["body"] if body else None


def parse_unified_diff(diff_text):
    """Map lines of a unified diff to (type, old_line, new_line).

    Returns {'new': {new_line: (type, old_line)}, 'old': {old_line: (type, new_line)}} where type is
    'add', 'del' or 'ctx'. For added lines old_line is GitLab's running old counter (used in line codes).
    """
    new, old = {}, {}
    o = n = 0
    for line in diff_text.splitlines():
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            o, n = int(m.group(1)), int(m.group(2))
            continue
        if not o and not n:
            continue
        if line.startswith("+"):
            new[n] = ("add", o)
            n += 1
        elif line.startswith("-"):
            old[o] = ("del", n)
            o += 1
        elif line.startswith("\\"):
            continue
        else:
            new[n] = ("ctx", o)
            old[o] = ("ctx", n)
            o += 1
            n += 1
    return {"new": new, "old": old}


def line_code(path, old_line, new_line):
    return f"{hashlib.sha1(path.encode()).hexdigest()}_{old_line}_{new_line}"


def diff_position(comment, refs, file_diff):
    """Build a GitLab text position for a GitHub review comment, or None if it cannot be anchored.

    refs: {'base_sha','start_sha','head_sha'} of the MR version; file_diff: GitLab diff entry for the file.
    """
    line = comment.get("line")
    if not line or not file_diff or not file_diff.get("diff"):
        return None
    side = comment.get("side") or "RIGHT"
    table = parse_unified_diff(file_diff["diff"])
    old_path, new_path = file_diff["old_path"], file_diff["new_path"]

    def point(ln, sd):
        if sd == "RIGHT":
            hit = table["new"].get(ln)
            if not hit:
                return None
            kind, o = hit
            return {"new_line": ln, "old_line": o if kind == "ctx" else None, "code_old": o, "code_new": ln,
                    "type": "new" if kind == "add" else None}
        hit = table["old"].get(ln)
        if not hit:
            return None
        kind, n = hit
        return {"old_line": ln, "new_line": n if kind == "ctx" else None, "code_old": ln, "code_new": n,
                "type": "old" if kind == "del" else None}

    end = point(line, side)
    if not end:
        return None
    pos = {"position_type": "text", "base_sha": refs["base_sha"], "start_sha": refs["start_sha"],
           "head_sha": refs["head_sha"], "old_path": old_path, "new_path": new_path}
    if end["new_line"] is not None:
        pos["new_line"] = end["new_line"]
    if end["old_line"] is not None:
        pos["old_line"] = end["old_line"]
    start_line = comment.get("start_line")
    if start_line and start_line != line:
        start = point(start_line, comment.get("start_side") or side)
        if start:
            def lr(p):
                return {"line_code": line_code(new_path, p["code_old"], p["code_new"]), "type": p["type"],
                        "old_line": p["old_line"], "new_line": p["new_line"]}
            pos["line_range"] = {"start": lr(start), "end": lr(end)}
    return pos


def fallback_review_body(comment):
    """Plain-note rendering of a review comment whose diff position cannot be anchored."""
    hunk = "\n".join((comment.get("diff_hunk") or "").splitlines()[-4:])
    where = comment.get("line") or comment.get("original_line") or "?"
    return f"**{comment.get('path')}** line {where}\n```diff\n{hunk}\n```\n\n{comment.get('body') or ''}"


GITHUB_ATTACHMENT = re.compile(
    r"https://(?:github\.com/user-attachments/(?:assets|files)/[^\s)\"'>\]]+"
    r"|(?:private-)?user-images\.githubusercontent\.com/[^\s)\"'>\]]+"
    r"|github\.com/[^/\s]+/[^/\s]+/(?:assets|files)/[^\s)\"'>\]]+)")


def find_attachments(text):
    return sorted(set(GITHUB_ATTACHMENT.findall(text or "")))
