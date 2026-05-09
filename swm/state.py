"""StateStore — JSONL append-only poll + thread store, organized by PR.

Layout under the configured state directory:

    <owner>/<repo>/pr-<N>/
        polls.jsonl                 # one line per poll for this PR (append-only)
        threads/
            <thread_id>.jsonl       # one line per poll for this Codex thread

All JSONL files are append-only — old records are NEVER rewritten. "Current"
state for a thread is the last line of its file; the full audit trail is the
whole file.

Locating data is path-driven: `state/frankyxhl/trinity/pr-49/` is everything
about that PR, delete-able with a single `rm -rf` to garbage-collect.

Construct `StateStore(some_dir)` for tests; use `default_store()` in the CLI.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from .models import BoxMiss, LedgerEntry, PollRecord, ThreadSnapshot

if TYPE_CHECKING:
    # Type-only import — runtime would cycle (swm.notify → swm.state for now_utc).
    from .notify import NotificationRecord

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = REPO_ROOT / "state"


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _read_jsonl(path: Path) -> Iterator[str]:
    """Yield non-blank lines from a JSONL file. Empty if the file is missing."""
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield line


class StateStore:
    """Filesystem-backed state. Each method is a thin file op — no locking."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.notifications_log = self.directory / "notifications.jsonl"

    # --- path helpers ---------------------------------------------------------

    def _pr_dir(self, repo: str, pr: int) -> Path:
        owner, name = repo.split("/", 1)
        return self.directory / owner / name / f"pr-{pr}"

    def _polls_path(self, repo: str, pr: int) -> Path:
        return self._pr_dir(repo, pr) / "polls.jsonl"

    def _thread_path(self, repo: str, pr: int, thread_id: str) -> Path:
        return self._pr_dir(repo, pr) / "threads" / f"{thread_id}.jsonl"

    def _ledger_path(self, repo: str, pr: int) -> Path:
        return self._pr_dir(repo, pr) / "ledger.jsonl"

    def _box_misses_path(self, repo: str, pr: int) -> Path:
        return self._pr_dir(repo, pr) / "box-misses.jsonl"

    # --- polls ---------------------------------------------------------------

    def append_poll(self, record: PollRecord) -> None:
        path = self._polls_path(record.repo, record.pr)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(record.model_dump_json() + "\n")

    def read_polls(self, repo: str | None = None, pr: int | None = None) -> Iterator[PollRecord]:
        for path in self._iter_polls_paths(repo, pr):
            for line in _read_jsonl(path):
                yield PollRecord.model_validate_json(line)

    def _iter_polls_paths(self, repo: str | None, pr: int | None) -> Iterator[Path]:
        """Yield every polls.jsonl matching the (repo, pr) filter."""
        if not self.directory.exists():
            return
        if repo is not None and pr is not None:
            yield self._polls_path(repo, pr)
            return
        if repo is not None:
            owner, name = repo.split("/", 1)
            base = self.directory / owner / name
            if base.exists():
                yield from sorted(base.glob("pr-*/polls.jsonl"))
            return
        if pr is not None:
            # Filter by PR number across every repo.
            yield from sorted(self.directory.glob(f"*/*/pr-{pr}/polls.jsonl"))
            return
        # No filter — walk every PR in every repo.
        yield from sorted(self.directory.glob("*/*/pr-*/polls.jsonl"))

    def latest_poll(self, repo: str, pr: int) -> PollRecord | None:
        last: PollRecord | None = None
        for rec in self.read_polls(repo, pr):
            last = rec
        return last

    def latest_per_pr(self, repo: str) -> dict[int, PollRecord]:
        by_pr: dict[int, PollRecord] = {}
        for path in self._iter_polls_paths(repo, None):
            try:
                pr = int(path.parent.name.removeprefix("pr-"))
            except ValueError:
                continue
            for line in _read_jsonl(path):
                by_pr[pr] = PollRecord.model_validate_json(line)  # last wins
        return by_pr

    # --- threads -------------------------------------------------------------

    def write_thread(self, snapshot: ThreadSnapshot) -> None:
        """Append the snapshot as a new JSONL line — never overwrites prior history."""
        path = self._thread_path(snapshot.repo, snapshot.pr, snapshot.thread_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(snapshot.model_dump_json() + "\n")

    def read_thread(self, repo: str, pr: int, thread_id: str) -> ThreadSnapshot | None:
        """Most recent snapshot (last line of JSONL), or None when missing."""
        path = self._thread_path(repo, pr, thread_id)
        last_line = None
        for line in _read_jsonl(path):
            last_line = line
        return ThreadSnapshot.model_validate_json(last_line) if last_line else None

    def read_thread_history(self, repo: str, pr: int, thread_id: str) -> list[ThreadSnapshot]:
        """Every snapshot ever written for this thread, oldest-first."""
        return [
            ThreadSnapshot.model_validate_json(line)
            for line in _read_jsonl(self._thread_path(repo, pr, thread_id))
        ]

    # --- ledger (SWM-1103 audit trail of one-shot writes) --------------------

    def append_ledger(self, entry: LedgerEntry) -> None:
        """Append one Stage-3+ write record to ledger.jsonl. Never overwritten."""
        path = self._ledger_path(entry.repo, entry.pr)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(entry.model_dump_json() + "\n")

    def read_ledger(self, repo: str, pr: int) -> list[LedgerEntry]:
        """Every ledger entry for this PR, oldest-first. Empty when missing."""
        return [
            LedgerEntry.model_validate_json(line)
            for line in _read_jsonl(self._ledger_path(repo, pr))
        ]

    # --- notifications (CHG-1112 positive transitions) ---------------------

    def append_notification(self, note: "NotificationRecord") -> None:
        """Append one positive-transition notification to notifications.jsonl."""
        self.notifications_log.parent.mkdir(parents=True, exist_ok=True)
        with self.notifications_log.open("a") as f:
            f.write(note.model_dump_json() + "\n")

    # --- box misses (CHG-1105 classifier blind-spot visibility) -------------

    def append_box_miss(self, miss: BoxMiss) -> None:
        path = self._box_misses_path(miss.repo, miss.pr)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(miss.model_dump_json() + "\n")

    def read_box_misses(self, repo: str | None = None) -> Iterator[BoxMiss]:
        """Walk every box-misses.jsonl matching the optional repo filter."""
        for path in self._iter_box_misses_paths(repo):
            for line in _read_jsonl(path):
                yield BoxMiss.model_validate_json(line)

    def _iter_box_misses_paths(self, repo: str | None) -> Iterator[Path]:
        if not self.directory.exists():
            return
        if repo is not None:
            owner, name = repo.split("/", 1)
            base = self.directory / owner / name
            if base.exists():
                yield from sorted(base.glob("pr-*/box-misses.jsonl"))
            return
        yield from sorted(self.directory.glob("*/*/pr-*/box-misses.jsonl"))


def default_store() -> StateStore:
    return StateStore(DEFAULT_STATE_DIR)
