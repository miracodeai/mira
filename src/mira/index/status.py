"""In-memory indexing status tracker."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class RepoIndexStatus:
    repo: str
    status: str  # "indexing", "completed", "failed", "cancelled"
    files_total: int = 0
    files_done: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    cancel_requested: bool = False


class IndexingTracker:
    """Thread-safe tracker for indexing progress."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, RepoIndexStatus] = {}

    def start(self, repo: str, files_total: int = 0) -> RepoIndexStatus:
        with self._lock:
            job = self._jobs[repo] = RepoIndexStatus(
                repo=repo,
                status="indexing",
                files_total=files_total,
                started_at=time.time(),
            )
            return job

    def discard(self, job: RepoIndexStatus) -> None:
        """Retire a superseded entry without clearing a replacement job."""
        with self._lock:
            if self._jobs.get(job.repo) is job:
                del self._jobs[job.repo]

    def progress(self, repo: str, files_done: int) -> None:
        with self._lock:
            job = self._jobs.get(repo)
            if job:
                job.files_done = files_done

    def complete(
        self, repo: str, files_done: int, *, expected: RepoIndexStatus | None = None
    ) -> None:
        with self._lock:
            job = self._jobs.get(repo)
            if job and (expected is None or job is expected):
                job.status = "completed"
                job.files_done = files_done
                job.finished_at = time.time()

    def fail(self, repo: str, error: str, *, expected: RepoIndexStatus | None = None) -> None:
        with self._lock:
            job = self._jobs.get(repo)
            if job and (expected is None or job is expected):
                job.status = "failed"
                job.error = error
                job.finished_at = time.time()

    def request_cancel(self, repo: str) -> bool:
        """Mark a job as cancel-requested. Returns True if there was an active job."""
        with self._lock:
            job = self._jobs.get(repo)
            if not job or job.status != "indexing":
                return False
            job.cancel_requested = True
            return True

    def is_cancel_requested(self, repo: str) -> bool:
        with self._lock:
            job = self._jobs.get(repo)
            return bool(job and job.cancel_requested)

    def cancel(
        self, repo: str, files_done: int, *, expected: RepoIndexStatus | None = None
    ) -> None:
        """Mark a job as finished due to cancellation."""
        with self._lock:
            job = self._jobs.get(repo)
            if job and (expected is None or job is expected):
                job.status = "cancelled"
                job.files_done = files_done
                job.finished_at = time.time()

    def get_all(self) -> list[RepoIndexStatus]:
        with self._lock:
            return list(self._jobs.values())

    def get_active(self) -> list[RepoIndexStatus]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status == "indexing"]


# Global singleton
tracker = IndexingTracker()
