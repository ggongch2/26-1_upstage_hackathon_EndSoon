"""Thread-safe in-memory job state registry for real-time progress reporting."""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any


STAGES = ("queued", "upload", "parse", "glossary", "translate", "docx", "pdf", "done", "error")


@dataclass
class JobState:
    job_id: str
    stage: str = "queued"
    processed: int = 0
    total: int = 0
    message: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["elapsed"] = (self.finished_at or time.time()) - self.started_at
        return d


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._lock = threading.Lock()

    def create(self, job_id: str) -> JobState:
        with self._lock:
            state = JobState(job_id=job_id)
            self._jobs[job_id] = state
            return state

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **kwargs: Any) -> None:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return
            for k, v in kwargs.items():
                setattr(state, k, v)

    def set_stage(self, job_id: str, stage: str, *, total: int | None = None, processed: int = 0, message: str = "") -> None:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return
            state.stage = stage
            state.processed = processed
            if total is not None:
                state.total = total
            state.message = message

    def tick(self, job_id: str, *, total: int | None = None) -> None:
        """Increment processed counter — used by per-element progress callbacks."""
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return
            state.processed += 1
            if total is not None:
                state.total = total

    def finish(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return
            state.stage = "done"
            state.result = result
            state.finished_at = time.time()
            state.message = ""

    def fail(self, job_id: str, err: str) -> None:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return
            state.stage = "error"
            state.error = err
            state.finished_at = time.time()


REGISTRY = JobRegistry()
