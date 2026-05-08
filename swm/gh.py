"""Thin wrapper around the `gh` CLI.

GhClient methods always return parsed JSON (or None for 404 / explicit no-data).
Tests inject a FakeRunner via the `runner` constructor argument; the default
runner shells out to `gh` via subprocess.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable

PR_BODY_REACTIONS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reactions(first: 50) {
        nodes { content user { login } createdAt }
      }
    }
  }
}
"""

REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 50) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: 30) {
            nodes {
              databaseId
              author { login }
              body
              createdAt
              replyTo { databaseId }
            }
          }
        }
      }
    }
  }
}
"""

RESOLVE_MUTATION = """mutation($threadId: ID!) { resolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved resolvedBy { login } } } }"""
UNRESOLVE_MUTATION = """mutation($threadId: ID!) { unresolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved } } }"""


@dataclass(frozen=True)
class GhResult:
    returncode: int
    stdout: str
    stderr: str


def _default_runner(args: list[str]) -> GhResult:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    return GhResult(proc.returncode, proc.stdout, proc.stderr)


class GhCommandError(RuntimeError):
    """Non-404 gh CLI failure — the watchdog should mark the poll as `error`."""


class GhClient:
    def __init__(self, runner: Callable[[list[str]], GhResult] | None = None) -> None:
        self._run = runner or _default_runner

    # --- low-level helpers ---------------------------------------------------

    def _json(self, args: list[str], *, allow_404: bool = False) -> object:
        result = self._run(args)
        if result.returncode != 0:
            if allow_404 and ("HTTP 404" in result.stderr or "Not Found" in result.stderr or "Branch not protected" in result.stderr):
                return None
            raise GhCommandError(f"gh {' '.join(args)!r} failed: {result.stderr.strip()}")
        if not result.stdout.strip():
            return None
        return json.loads(result.stdout)

    # --- REST -----------------------------------------------------------------

    def list_open_prs(self, repo: str, *, base: str | None = None) -> list[dict]:
        args = [
            "pr", "list", "--repo", repo, "--state", "open",
            "--json", "number,title,url,isDraft,headRefOid,baseRefName,mergeStateStatus,reviewDecision,statusCheckRollup,updatedAt,author",
        ]
        if base:
            args += ["--base", base]
        data = self._json(args) or []
        return list(data)

    def view_pr(self, repo: str, pr: int, fields: list[str]) -> dict:
        args = ["pr", "view", str(pr), "--repo", repo, "--json", ",".join(fields)]
        data = self._json(args) or {}
        return dict(data)

    def pulls_comments(self, repo: str, pr: int) -> list[dict]:
        """Inline review comments (REST). Includes path, line, in_reply_to_id."""
        args = ["api", f"repos/{repo}/pulls/{pr}/comments", "--paginate"]
        data = self._json(args) or []
        return list(data)

    def issues_comments(self, repo: str, pr: int) -> list[dict]:
        """Issue-thread comments (REST). PRs are issues for this endpoint."""
        args = ["api", f"repos/{repo}/issues/{pr}/comments", "--paginate"]
        data = self._json(args) or []
        return list(data)

    def branch_protection(self, repo: str, branch: str) -> dict | None:
        """Returns None when branch has no protection rule (404 -> None)."""
        args = ["api", f"repos/{repo}/branches/{branch}/protection"]
        data = self._json(args, allow_404=True)
        return data if isinstance(data, dict) else None

    # --- GraphQL --------------------------------------------------------------

    def pr_body_reactions(self, repo: str, pr: int) -> list[dict]:
        """Reactions on the PR body itself. Codex bot uses these as a status signal:
        EYES = currently reviewing this head, THUMBS_UP = reviewed and approved."""
        owner, name = repo.split("/", 1)
        args = [
            "api", "graphql",
            "-f", f"query={PR_BODY_REACTIONS_QUERY}",
            "-F", f"owner={owner}",
            "-F", f"repo={name}",
            "-F", f"pr={pr}",
        ]
        data = self._json(args) or {}
        try:
            return list(data["data"]["repository"]["pullRequest"]["reactions"]["nodes"])
        except (KeyError, TypeError):
            return []

    def review_threads(self, repo: str, pr: int) -> list[dict]:
        owner, name = repo.split("/", 1)
        args = [
            "api", "graphql",
            "-f", f"query={REVIEW_THREADS_QUERY}",
            "-F", f"owner={owner}",
            "-F", f"repo={name}",
            "-F", f"pr={pr}",
        ]
        data = self._json(args) or {}
        try:
            return list(data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"])
        except (KeyError, TypeError):
            return []

    def resolve_thread(self, thread_id: str) -> dict:
        """Stage 1.5 mutation. Caller is responsible for verifying local verdict=RESOLVED."""
        args = ["api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-F", f"threadId={thread_id}"]
        data = self._json(args) or {}
        return dict(data.get("data", {}).get("resolveReviewThread", {}).get("thread", {}))

    def unresolve_thread(self, thread_id: str) -> dict:
        args = ["api", "graphql", "-f", f"query={UNRESOLVE_MUTATION}", "-F", f"threadId={thread_id}"]
        data = self._json(args) or {}
        return dict(data.get("data", {}).get("unresolveReviewThread", {}).get("thread", {}))

    # --- SWM-1103 one-shot writes -------------------------------------------

    def auth_active_login(self) -> str:
        """Return the active gh account login. Raises GhCommandError on parse failure."""
        result = self._run(["auth", "status"])
        if result.returncode != 0:
            raise GhCommandError(f"gh auth status failed: {result.stderr.strip() or result.stdout.strip()}")
        # gh prints to stderr historically; merging both is robust across versions.
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        current_account: str | None = None
        for line in text.splitlines():
            m = re.search(r"Logged in to .+ account (\S+)", line)
            if m:
                current_account = m.group(1)
            if "Active account: true" in line and current_account:
                return current_account
        raise GhCommandError("could not determine active gh account from `gh auth status` output")

    def submit_review_approve(self, repo: str, pr: int, body: str) -> dict:
        """Stage-3 — caller is responsible for SWM-1103 gates. Returns raw stdout dict."""
        result = self._run(["pr", "review", str(pr), "--repo", repo, "--approve", "--body", body])
        if result.returncode != 0:
            raise GhCommandError(f"gh pr review --approve failed: {result.stderr.strip()}")
        return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip()}

    def edit_pr_body(self, repo: str, pr: int, body: str) -> dict:
        """Stage-3 — replaces the entire PR body via --body-file (no arg expansion).
        Caller is responsible for ensuring the new body matches an audited diff."""
        fd, path = tempfile.mkstemp(suffix=".md", prefix="swm-pr-body-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(body)
            result = self._run(["pr", "edit", str(pr), "--repo", repo, "--body-file", path])
            if result.returncode != 0:
                raise GhCommandError(f"gh pr edit --body-file failed: {result.stderr.strip()}")
            return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
