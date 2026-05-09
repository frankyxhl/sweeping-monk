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
from urllib.parse import quote

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


def _default_runner(args: list[str], *, token: str | None = None) -> GhResult:
    env = None
    if token:
        env = os.environ.copy()
        env["GH_TOKEN"] = token
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, env=env)
    return GhResult(proc.returncode, proc.stdout, proc.stderr)


class GhCommandError(RuntimeError):
    """Non-404 gh CLI failure — the watchdog should mark the poll as `error`."""


class GhClient:
    def __init__(
        self,
        runner: Callable[[list[str]], GhResult] | None = None,
        *,
        token: str | None = None,
        actor_login: str | None = None,
    ) -> None:
        self._token = token
        self._actor_login = actor_login
        self._run = runner or (lambda args: _default_runner(args, token=token))

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

    def _paginated_list(self, args: list[str]) -> list[dict]:
        """Run a paginated gh api call and return a flat list.

        gh api --paginate without --slurp concatenates raw JSON arrays
        (e.g. [...][...]), which json.loads rejects. --slurp wraps pages
        into [[page1...], [page2...]], which is valid JSON; we flatten it.
        """
        result = self._run([*args, "--paginate", "--slurp"])
        if result.returncode != 0:
            raise GhCommandError(f"gh {' '.join(args)!r} failed: {result.stderr.strip()}")
        if not result.stdout.strip():
            return []
        data = json.loads(result.stdout)
        if data and isinstance(data[0], list):
            return [item for page in data for item in page]
        return list(data)

    def pulls_comments(self, repo: str, pr: int) -> list[dict]:
        """Inline review comments (REST). Includes path, line, in_reply_to_id."""
        return self._paginated_list(["api", f"repos/{repo}/pulls/{pr}/comments"])

    def issues_comments(self, repo: str, pr: int) -> list[dict]:
        """Issue-thread comments (REST). PRs are issues for this endpoint."""
        return self._paginated_list(["api", f"repos/{repo}/issues/{pr}/comments"])

    @property
    def actor_login(self) -> str | None:
        return self._actor_login

    def _write_json_payload(self, args: list[str], payload: dict, *, prefix: str) -> dict:
        fd, path = tempfile.mkstemp(suffix=".json", prefix=prefix)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            result = self._run([*args, "--input", path])
            if result.returncode != 0:
                raise GhCommandError(f"gh api {' '.join(args)!r} failed: {result.stderr.strip()}")
            return json.loads(result.stdout) if result.stdout.strip() else {}
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def create_issue_comment(self, repo: str, issue_number: int, body: str) -> dict:
        """Append a new issue/PR timeline comment."""
        return self._write_json_payload(
            [
                "api", f"repos/{repo}/issues/{issue_number}/comments",
                "--method", "POST",
            ],
            {"body": body},
            prefix="swm-issue-comment-",
        )

    def reply_to_review_comment(self, repo: str, pr: int, comment_id: int, body: str) -> dict:
        """Reply to a pull-request review comment before resolving its thread."""
        return self._write_json_payload(
            [
                "api", f"repos/{repo}/pulls/{pr}/comments/{comment_id}/replies",
                "--method", "POST",
            ],
            {"body": body},
            prefix="swm-review-reply-",
        )

    def review_comment_reactions(
        self,
        repo: str,
        comment_id: int,
        *,
        content: str | None = None,
    ) -> list[dict]:
        """Reactions on a pull-request review comment."""
        path = f"repos/{repo}/pulls/comments/{comment_id}/reactions?per_page=100"
        if content:
            path = f"{path}&content={quote(content, safe='')}"
        data = self._json(["api", path]) or []
        return list(data)

    def _current_actor_login(self) -> str:
        return self._actor_login or self.auth_active_login()

    def remove_review_comment_reaction(self, repo: str, comment_id: int, content: str) -> list[dict]:
        """Remove this actor's matching reactions from a review comment."""
        owner, name = repo.split("/", 1)
        actor_login = self._current_actor_login()
        removed: list[dict] = []
        for reaction in self.review_comment_reactions(repo, comment_id, content=content):
            user = reaction.get("user") or {}
            if user.get("login") != actor_login:
                continue
            reaction_id = reaction.get("id")
            if not reaction_id:
                continue
            result = self._run([
                "api",
                f"repos/{owner}/{name}/pulls/comments/{comment_id}/reactions/{reaction_id}",
                "--method",
                "DELETE",
            ])
            if result.returncode != 0:
                raise GhCommandError(f"gh api delete review-comment reaction failed: {result.stderr.strip()}")
            removed.append(reaction)
        return removed

    def add_review_comment_reaction(self, repo: str, comment_id: int, content: str) -> dict:
        """Add this actor's reaction to a review comment, idempotently."""
        actor_login = self._current_actor_login()
        for reaction in self.review_comment_reactions(repo, comment_id, content=content):
            user = reaction.get("user") or {}
            if user.get("login") == actor_login:
                return {**reaction, "already_exists": True}
        return self._write_json_payload(
            [
                "api", f"repos/{repo}/pulls/comments/{comment_id}/reactions",
                "--method", "POST",
            ],
            {"content": content},
            prefix="swm-review-reaction-",
        )

    def set_review_comment_reaction(self, repo: str, comment_id: int, content: str) -> dict:
        """Set this actor's review-comment status reaction.

        Clearance uses +1 for resolved and -1 for still-open. Remove the
        opposite signal first so the original Codex comment has one current
        Clearance verdict.
        """
        if content not in {"+1", "-1"}:
            raise ValueError("review-comment verdict reaction must be '+1' or '-1'")
        opposite = "-1" if content == "+1" else "+1"
        removed = self.remove_review_comment_reaction(repo, comment_id, opposite)
        added = self.add_review_comment_reaction(repo, comment_id, content)
        return {"content": content, "added": added, "removed": removed}

    def pr_diff(self, repo: str, pr: int) -> str:
        """Unified diff for the PR. Used only by optional LLM investigation."""
        result = self._run(["pr", "diff", str(pr), "--repo", repo])
        if result.returncode != 0:
            raise GhCommandError(f"gh pr diff failed: {result.stderr.strip()}")
        return result.stdout

    def branch_protection(self, repo: str, branch: str) -> dict | None:
        """Returns None only when the branch is confirmed unprotected (HTTP 404).

        A 403 "Resource not accessible by integration" means the GitHub App
        lacks Administration read permission — protection status is unknown.
        Return a sentinel {"_unknown": True} so callers see a non-None value
        and treat the branch as protected (fail-safe). This prevents severity
        demotion on branches that may well have required checks configured.
        """
        args = ["api", f"repos/{repo}/branches/{branch}/protection"]
        result = self._run(args)
        if result.returncode != 0:
            if (
                "HTTP 404" in result.stderr
                or "Not Found" in result.stderr
                or "Branch not protected" in result.stderr
            ):
                return None
            if (
                "HTTP 403" in result.stderr
                and "Resource not accessible by integration" in result.stderr
            ):
                return {"_unknown": True}
            raise GhCommandError(f"gh {' '.join(args)!r} failed: {result.stderr.strip()}")
        if not result.stdout.strip():
            return None
        data = json.loads(result.stdout)
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
        if self._actor_login:
            return self._actor_login
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

    def submit_review_approve(
        self, repo: str, pr: int, body: str, *, commit_id: str | None = None,
    ) -> dict:
        """Stage-3 — caller is responsible for SWM-1103 gates. Returns raw stdout dict.

        Body passes via --body-file (tempfile) so arbitrary maintainer text is never
        subject to shell quoting / ARG_MAX. Mirrors edit_pr_body.
        """
        if commit_id:
            return self._submit_review_approve_api(repo, pr, body, commit_id=commit_id)
        fd, path = tempfile.mkstemp(suffix=".md", prefix="swm-review-body-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(body)
            result = self._run(["pr", "review", str(pr), "--repo", repo, "--approve", "--body-file", path])
            if result.returncode != 0:
                raise GhCommandError(f"gh pr review --approve failed: {result.stderr.strip()}")
            return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _submit_review_approve_api(
        self, repo: str, pr: int, body: str, *, commit_id: str,
    ) -> dict:
        """Submit APPROVE via REST through `gh api`.

        The webhook daemon uses this path with a GitHub App installation token.
        Binding the review to `commit_id` keeps the approve operation anchored to
        the head SHA that the watchdog just verified.
        """
        fd, path = tempfile.mkstemp(suffix=".json", prefix="swm-review-payload-")
        try:
            payload = {"event": "APPROVE", "body": body, "commit_id": commit_id}
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            result = self._run([
                "api", f"repos/{repo}/pulls/{pr}/reviews",
                "--method", "POST",
                "--input", path,
            ])
            if result.returncode != 0:
                raise GhCommandError(f"gh api create review failed: {result.stderr.strip()}")
            return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

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
