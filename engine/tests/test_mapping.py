from ghmirror import mapping


def test_usernames():
    assert mapping.username_for("octocat") == "octocat"
    assert mapping.username_for("dependabot[bot]") == "dependabot-bot"
    assert mapping.username_for("ghost") == "ghost-gh"
    assert mapping.username_for("root") == "root-gh"
    assert mapping.username_for("weird.git") == "weird.git-gh"


def test_email():
    assert mapping.email_for("Octo", "m.example") == "octo@m.example"
    assert mapping.email_for("Octo", "m.example", {"Octo": "o@corp.com"}) == "o@corp.com"
    assert mapping.email_for("renovate[bot]", "m.example") == "renovate-bot@m.example"


def test_visibility_cap():
    assert mapping.visibility({"visibility": "public"}, "private") == "private"
    assert mapping.visibility({"visibility": "public"}, "public") == "public"
    assert mapping.visibility({"private": True}, "public") == "private"
    assert mapping.visibility({"visibility": "internal"}, "internal") == "internal"


def test_project_path():
    assert mapping.project_path("my.repo") == "my.repo"
    assert mapping.project_path(".github") == "_github"
    assert mapping.project_path("x.git") == "x.git-gh"


def test_iso_and_states():
    assert mapping.iso("2026-10-31T04:35:46Z") == "2026-10-31T04:35:46Z"
    assert mapping.iso("2026-10-31T10:05:46.123+05:30") == "2026-10-31T04:35:46Z"
    assert mapping.pr_state({"state": "closed", "merged_at": "x"}) == "merged"
    assert mapping.pr_state({"state": "closed", "merged_at": None}) == "closed"
    assert mapping.pr_state({"state": "open"}) == "opened"
    assert mapping.mr_title({"title": "x", "draft": True}) == "Draft: x"
    assert mapping.mr_title({"title": "Draft: x", "draft": True}) == "Draft: x"


def test_review_bodies():
    assert mapping.review_note_body({"state": "COMMENTED", "body": ""}) is None
    assert mapping.review_note_body({"state": "COMMENTED", "body": "hi"}) == "hi"
    assert mapping.review_note_body({"state": "APPROVED", "body": ""}) is None
    assert "Requested changes" in mapping.review_note_body({"state": "CHANGES_REQUESTED", "body": ""})
    assert mapping.review_note_body({"state": "PENDING", "body": "x"}) is None


DIFF = """@@ -3,8 +3,8 @@
 line 3
 line 4
-line 5
+line 5 changed in feat-merge
 line 6
 line 7
 line 8
 line 9
-line 10
+line 10 changed in feat-merge
"""


def test_parse_diff_and_positions():
    t = mapping.parse_unified_diff(DIFF)
    assert t["new"][5] == ("add", 6)        # added line, old counter already past the removed line 5
    assert t["old"][5] == ("del", 5)
    assert t["new"][4] == ("ctx", 4)
    refs = {"base_sha": "b", "start_sha": "s", "head_sha": "h"}
    fd = {"diff": DIFF, "old_path": "app.txt", "new_path": "app.txt"}
    pos = mapping.diff_position({"line": 5, "side": "RIGHT"}, refs, fd)
    assert pos["new_line"] == 5 and "old_line" not in pos
    ctx = mapping.diff_position({"line": 4, "side": "RIGHT"}, refs, fd)
    assert ctx["new_line"] == 4 and ctx["old_line"] == 4
    left = mapping.diff_position({"line": 5, "side": "LEFT"}, refs, fd)
    assert left["old_line"] == 5 and "new_line" not in left
    multi = mapping.diff_position({"line": 10, "start_line": 9, "side": "RIGHT"}, refs, fd)
    assert multi["line_range"]["start"]["new_line"] == 9 and multi["line_range"]["end"]["type"] == "new"
    assert multi["line_range"]["start"]["line_code"].endswith("_9_9")
    assert mapping.diff_position({"line": 40, "side": "RIGHT"}, refs, fd) is None
    assert mapping.diff_position({"line": None}, refs, fd) is None


def test_attachments():
    body = "see ![x](https://github.com/user-attachments/assets/abc-123) and https://example.com/a.png"
    assert mapping.find_attachments(body) == ["https://github.com/user-attachments/assets/abc-123"]


def test_package_names():
    assert mapping.package_version("v1.0") == "v1.0"
    assert mapping.package_version("release/2024") == "release_2024"
    assert mapping.package_filename("my file (1).zip") == "my_file__1_.zip"
