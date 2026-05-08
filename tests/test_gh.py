"""Unit tests for GhClient — uses an injected runner returning canned outputs."""
from __future__ import annotations

import json

import pytest

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
