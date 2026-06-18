"""In-memory review status tracker for active/in-flight reviews."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class ReviewStatus:
    repo: str  # "owner/repo"
    pr_number: int
    pr_title: str
    pr_url: str
    status: str  # "reviewing", "completed", "failed"
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""


class ReviewTracker:
    """Thread-safe tracker for active review jobs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, ReviewStatus] = {}

    def _key(self, repo: str, pr_number: int) -> str:
        return f"{repo}#{pr_number}"

    def start(self, repo: str, pr_number: int, pr_title: str, pr_url: str) -> None:
        with self._lock:
            key = self._key(repo, pr_number)
            self._jobs[key] = ReviewStatus(
                repo=repo,
                pr_number=pr_number,
                pr_title=pr_title,
                pr_url=pr_url,
                status="reviewing",
                started_at=time.time(),
            )

    def complete(self, repo: str, pr_number: int) -> None:
        with self._lock:
            key = self._key(repo, pr_number)
            job = self._jobs.get(key)
            if job:
                job.status = "completed"
                job.finished_at = time.time()

    def fail(self, repo: str, pr_number: int, error: str = "") -> None:
        with self._lock:
            key = self._key(repo, pr_number)
            job = self._jobs.get(key)
            if job:
                job.status = "failed"
                job.error = error
                job.finished_at = time.time()

    def get_active(self) -> list[ReviewStatus]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status == "reviewing"]

    def get_all(self) -> list[ReviewStatus]:
        with self._lock:
            return list(self._jobs.values())


tracker = ReviewTracker()
