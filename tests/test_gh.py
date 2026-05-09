"""Unit tests for GhClient — uses an injected runner returning canned outputs."""
from __future__ import annotations

import json

import pytest

from swm import gh as gh_module
from swm.gh import GhClient, GhCommandError, GhResult


class StubRunner:
    """Fakes the gh subprocess by matching args prefixes to canned outputs."""
    def __init__(self) -> None:
        self.responses: dict[tuple, GhResult] = {}
        self.calls: list[list[str]] = []

    def expect(self, args_prefix: tuple, *, stdout: str = "", stderr: str = "", code: int = 0) -> None:
        self.responses[args_prefix] = GhResult(returncode=code, stdout=stdout, stderr=stderr)

    def __call__(self, args: list[str]) -> GhResult:
        self.calls.append(args)
        for prefix, response in self.responses.items():
            if tuple(args[: len(prefix)]) == prefix:
                return response
        return GhResult(returncode=1, stdout="", stderr=f"no canned response for {args}")


def test_list_open_prs_invokes_pr_list_with_repo() -> None:
    # Arrange
    runner = StubRunner()
    runner.expect(("pr", "list"), stdout=json.dumps([{"number": 1, "title": "x"}]))
    client = GhClient(runner=runner)

    # Act
    prs = client.list_open_prs("owner/repo")

    # Assert
    assert prs == [{"number": 1, "title": "x"}]
    invoked = runner.calls[0]
    assert "--repo" in invoked
    assert invoked[invoked.index("--repo") + 1] == "owner/repo"
    assert "--state" in invoked and invoked[invoked.index("--state") + 1] == "open"


def test_list_open_prs_returns_empty_when_no_prs() -> None:
    # Arrange
    runner = StubRunner()
    runner.expect(("pr", "list"), stdout="[]")
    client = GhClient(runner=runner)

    # Act / Assert
    assert client.list_open_prs("owner/repo") == []


def test_view_pr_passes_fields_as_csv() -> None:
    # Arrange
    runner = StubRunner()
    runner.expect(("pr", "view"), stdout=json.dumps({"number": 49, "title": "ci: ..."}))
    client = GhClient(runner=runner)

    # Act
    result = client.view_pr("owner/repo", 49, ["number", "title"])

    # Assert
    assert result["number"] == 49
    fields_arg = runner.calls[0][runner.calls[0].index("--json") + 1]
    assert fields_arg == "number,title"


def test_branch_protection_returns_none_on_404() -> None:
    # Arrange
    runner = StubRunner()
    runner.expect(("api", "repos/owner/repo/branches/main/protection"),
                  code=1, stderr="HTTP 404: Branch not protected")
    client = GhClient(runner=runner)

    # Act / Assert
    assert client.branch_protection("owner/repo", "main") is None


def test_branch_protection_returns_sentinel_when_app_token_cannot_read_protection() -> None:
    # A 403 must NOT return None (which would set branch_protected=False).
    # It must return a non-None sentinel so callers treat the branch as protected.
    runner = StubRunner()
    runner.expect(("api", "repos/owner/repo/branches/main/protection"),
                  code=1, stderr="gh: Resource not accessible by integration (HTTP 403)")
    client = GhClient(runner=runner)

    result = client.branch_protection("owner/repo", "main")
    assert result is not None
    assert result.get("_unknown") is True


def test_branch_protection_returns_dict_when_protected() -> None:
    # Arrange
    runner = StubRunner()
    runner.expect(("api", "repos/owner/repo/branches/main/protection"),
                  stdout=json.dumps({"required_status_checks": {"contexts": ["ci"]}}))
    client = GhClient(runner=runner)

    # Act
    protection = client.branch_protection("owner/repo", "main")

    # Assert
    assert protection is not None
    assert protection["required_status_checks"]["contexts"] == ["ci"]


def test_review_threads_unwraps_graphql_envelope() -> None:
    # Arrange
    payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [
        {"id": "T1", "isResolved": False, "isOutdated": False, "comments": {"nodes": []}},
    ]}}}}}
    runner = StubRunner()
    runner.expect(("api", "graphql"), stdout=json.dumps(payload))
    client = GhClient(runner=runner)

    # Act
    threads = client.review_threads("owner/repo", 49)

    # Assert
    assert len(threads) == 1
    assert threads[0]["id"] == "T1"


def test_review_threads_handles_missing_pr_key() -> None:
    # Arrange — PR doesn't exist; GraphQL returns null pullRequest
    payload = {"data": {"repository": {"pullRequest": None}}}
    runner = StubRunner()
    runner.expect(("api", "graphql"), stdout=json.dumps(payload))
    client = GhClient(runner=runner)

    # Act / Assert — does not raise
    assert client.review_threads("owner/repo", 99) == []


def test_resolve_thread_unwraps_thread_payload() -> None:
    # Arrange
    payload = {"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True, "resolvedBy": {"login": "tester"}}}}}
    runner = StubRunner()
    runner.expect(("api", "graphql"), stdout=json.dumps(payload))
    client = GhClient(runner=runner)

    # Act
    result = client.resolve_thread("T1")

    # Assert
    assert result["isResolved"] is True
    assert result["resolvedBy"]["login"] == "tester"


def test_unresolve_thread_unwraps_thread_payload() -> None:
    # Arrange
    payload = {"data": {"unresolveReviewThread": {"thread": {"id": "T1", "isResolved": False}}}}
    runner = StubRunner()
    runner.expect(("api", "graphql"), stdout=json.dumps(payload))
    client = GhClient(runner=runner)

    # Act
    result = client.unresolve_thread("T1")

    # Assert
    assert result["isResolved"] is False


def test_reply_to_review_comment_uses_rest_payload_file() -> None:
    runner = StubRunner()
    runner.expect(
        ("api", "repos/frankyxhl/alfred/pulls/122/comments/321/replies"),
        stdout='{"id":654,"body":"ok"}',
    )
    gh = GhClient(runner=runner)

    out = gh.reply_to_review_comment("frankyxhl/alfred", 122, 321, "Close reason with `quotes`.")

    assert out["id"] == 654
    submitted = runner.calls[-1]
    assert submitted[:2] == ["api", "repos/frankyxhl/alfred/pulls/122/comments/321/replies"]
    assert submitted[submitted.index("--method") + 1] == "POST"
    payload_path = submitted[submitted.index("--input") + 1]
    import os as _os
    assert not _os.path.exists(payload_path), "payload tempfile must be cleaned up"


def test_create_issue_comment_appends_new_timeline_comment() -> None:
    runner = StubRunner()
    runner.expect(
        ("api", "repos/frankyxhl/alfred/issues/123/comments"),
        stdout='{"id":777,"html_url":"https://github.test/comment"}',
    )
    gh = GhClient(runner=runner)

    out = gh.create_issue_comment("frankyxhl/alfred", 123, "Clearance run conclusion")

    assert out["id"] == 777
    submitted = runner.calls[-1]
    assert submitted[:2] == ["api", "repos/frankyxhl/alfred/issues/123/comments"]
    assert submitted[submitted.index("--method") + 1] == "POST"
    payload_path = submitted[submitted.index("--input") + 1]
    import os as _os
    assert not _os.path.exists(payload_path), "payload tempfile must be cleaned up"


def test_set_review_comment_reaction_replaces_opposite_status() -> None:
    runner = StubRunner()
    runner.expect(
        ("api", "repos/frankyxhl/alfred/pulls/comments/321/reactions?per_page=100&content=-1"),
        stdout=json.dumps([{"id": 91, "content": "-1", "user": {"login": "iterwheel-clearance[bot]"}}]),
    )
    runner.expect(
        ("api", "repos/frankyxhl/alfred/pulls/comments/321/reactions/91"),
        stdout="",
    )
    runner.expect(
        ("api", "repos/frankyxhl/alfred/pulls/comments/321/reactions?per_page=100&content=%2B1"),
        stdout="[]",
    )
    runner.expect(
        ("api", "repos/frankyxhl/alfred/pulls/comments/321/reactions"),
        stdout=json.dumps({"id": 92, "content": "+1"}),
    )
    gh = GhClient(runner=runner, actor_login="iterwheel-clearance[bot]")

    out = gh.set_review_comment_reaction("frankyxhl/alfred", 321, "+1")

    assert out["content"] == "+1"
    assert out["removed"][0]["id"] == 91
    assert out["added"]["id"] == 92
    delete_call = runner.calls[1]
    assert delete_call[:2] == ["api", "repos/frankyxhl/alfred/pulls/comments/321/reactions/91"]
    assert delete_call[delete_call.index("--method") + 1] == "DELETE"
    post_call = runner.calls[-1]
    assert post_call[:2] == ["api", "repos/frankyxhl/alfred/pulls/comments/321/reactions"]
    assert post_call[post_call.index("--method") + 1] == "POST"


def test_add_review_comment_reaction_is_idempotent_for_same_actor() -> None:
    runner = StubRunner()
    runner.expect(
        ("api", "repos/frankyxhl/alfred/pulls/comments/321/reactions?per_page=100&content=%2B1"),
        stdout=json.dumps([{"id": 92, "content": "+1", "user": {"login": "iterwheel-clearance[bot]"}}]),
    )
    gh = GhClient(runner=runner, actor_login="iterwheel-clearance[bot]")

    out = gh.add_review_comment_reaction("frankyxhl/alfred", 321, "+1")

    assert out["already_exists"] is True
    assert len(runner.calls) == 1


def test_command_error_raises_for_non_404_failures() -> None:
    # Arrange
    runner = StubRunner()
    runner.expect(("pr", "list"), code=1, stderr="rate limit exceeded")
    client = GhClient(runner=runner)

    # Act / Assert
    with pytest.raises(GhCommandError, match="rate limit"):
        client.list_open_prs("owner/repo")


# --- SWM-1104 / SWM-1103 one-shot writes -------------------------------------


AUTH_STATUS_OUTPUT = """github.com
  ✓ Logged in to github.com account ryosaeba1985 (keyring)
  - Active account: true
  - Token: gho_*****
  ✓ Logged in to github.com account frankyxhl (keyring)
  - Active account: false
"""


def test_auth_active_login_parses_active_account() -> None:
    runner = StubRunner()
    runner.expect(("auth", "status"), stdout=AUTH_STATUS_OUTPUT)
    gh = GhClient(runner=runner)
    assert gh.auth_active_login() == "ryosaeba1985"


def test_auth_active_login_falls_back_to_stderr() -> None:
    """gh emits the status to stderr in some versions."""
    runner = StubRunner()
    runner.expect(("auth", "status"), stderr=AUTH_STATUS_OUTPUT)
    gh = GhClient(runner=runner)
    assert gh.auth_active_login() == "ryosaeba1985"


def test_auth_active_login_raises_when_unparseable() -> None:
    runner = StubRunner()
    runner.expect(("auth", "status"), stdout="totally garbled")
    gh = GhClient(runner=runner)
    with pytest.raises(GhCommandError, match="could not determine active gh account"):
        gh.auth_active_login()


def test_auth_active_login_can_be_supplied_for_app_actor() -> None:
    gh = GhClient(runner=lambda args: GhResult(1, "", "should not run"), actor_login="iterwheel-clearance[bot]")
    assert gh.auth_active_login() == "iterwheel-clearance[bot]"


def test_default_runner_sets_gh_token_env(monkeypatch) -> None:
    seen = {}

    class Proc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(cmd, *, capture_output, text, env=None):
        seen["cmd"] = cmd
        seen["env"] = env
        return Proc()

    monkeypatch.setattr(gh_module.subprocess, "run", fake_run)
    result = gh_module._default_runner(["api", "user"], token="installation-token")

    assert result.returncode == 0
    assert seen["cmd"] == ["gh", "api", "user"]
    assert seen["env"]["GH_TOKEN"] == "installation-token"


def test_submit_review_approve_uses_body_file_not_arg_expansion() -> None:
    """SWM-1104 fix: arbitrary maintainer text must go through --body-file (no shell expansion)."""
    runner = StubRunner()
    runner.expect(("pr", "review", "66"), stdout="approved")
    gh = GhClient(runner=runner)
    out = gh.submit_review_approve("frankyxhl/trinity", 66, body="Approved with `backticks` and 'quotes'.")
    assert out["stdout"] == "approved"
    submitted = runner.calls[-1]
    assert "--approve" in submitted
    assert "--body-file" in submitted
    assert "--body" not in submitted  # only --body-file, not --body
    body_file_path = submitted[submitted.index("--body-file") + 1]
    import os as _os
    assert not _os.path.exists(body_file_path), "tempfile must be cleaned up after the call"


def test_submit_review_approve_raises_on_gh_failure() -> None:
    runner = StubRunner()
    runner.expect(("pr", "review", "66"), code=1, stderr="GraphQL: Could not resolve")
    gh = GhClient(runner=runner)
    with pytest.raises(GhCommandError, match="gh pr review --approve failed"):
        gh.submit_review_approve("frankyxhl/trinity", 66, body="x")


def test_submit_review_approve_with_commit_id_uses_rest_payload_file() -> None:
    runner = StubRunner()
    runner.expect(("api", "repos/frankyxhl/trinity/pulls/66/reviews"), stdout='{"state":"APPROVED"}')
    gh = GhClient(runner=runner)

    out = gh.submit_review_approve(
        "frankyxhl/trinity",
        66,
        body="Approved by Clearance",
        commit_id="abc123",
    )

    assert "APPROVED" in out["stdout"]
    submitted = runner.calls[-1]
    assert submitted[:2] == ["api", "repos/frankyxhl/trinity/pulls/66/reviews"]
    assert "--method" in submitted
    assert submitted[submitted.index("--method") + 1] == "POST"
    payload_path = submitted[submitted.index("--input") + 1]
    import os as _os
    assert not _os.path.exists(payload_path), "payload tempfile must be cleaned up"


def test_edit_pr_body_uses_body_file_and_cleans_up(tmp_path) -> None:
    """The temp file passed to gh pr edit must be deleted after the call."""
    runner = StubRunner()
    runner.expect(("pr", "edit", "66"), stdout="edited")
    gh = GhClient(runner=runner)
    out = gh.edit_pr_body("frankyxhl/trinity", 66, body="new body content")
    assert out["stdout"] == "edited"
    submitted = runner.calls[-1]
    assert "--body-file" in submitted
    body_file_path = submitted[submitted.index("--body-file") + 1]
    # Temp file should have been cleaned up
    import os
    assert not os.path.exists(body_file_path)


def test_edit_pr_body_raises_on_gh_failure() -> None:
    runner = StubRunner()
    runner.expect(("pr", "edit", "66"), code=1, stderr="permission denied")
    gh = GhClient(runner=runner)
    with pytest.raises(GhCommandError, match="gh pr edit --body-file failed"):
        gh.edit_pr_body("frankyxhl/trinity", 66, body="x")


def test_pulls_comments_flattens_paginated_slurp_output() -> None:
    # gh api --paginate --slurp wraps multiple pages into [[page1...], [page2...]].
    # _paginated_list must flatten to a single list; json.loads on bare concatenation fails.
    page1 = [{"id": 1, "body": "first"}]
    page2 = [{"id": 2, "body": "second"}, {"id": 3, "body": "third"}]
    import json as _json
    slurp_output = _json.dumps([page1, page2])

    runner = StubRunner()
    runner.expect(
        ("api", "repos/owner/repo/pulls/7/comments"),
        stdout=slurp_output,
    )
    gh = GhClient(runner=runner)
    result = gh.pulls_comments("owner/repo", 7)

    assert len(result) == 3
    assert result[0]["id"] == 1
    assert result[2]["id"] == 3


def test_pulls_comments_handles_single_page_slurp_output() -> None:
    # When only one page exists, --slurp still wraps it in an outer list.
    import json as _json
    single_page = [{"id": 10, "body": "only"}]
    runner = StubRunner()
    runner.expect(
        ("api", "repos/owner/repo/pulls/8/comments"),
        stdout=_json.dumps([single_page]),
    )
    gh = GhClient(runner=runner)
    result = gh.pulls_comments("owner/repo", 8)
    assert len(result) == 1
    assert result[0]["id"] == 10
