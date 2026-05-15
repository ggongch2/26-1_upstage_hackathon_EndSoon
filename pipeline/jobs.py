"""Thread-safe in-memory job state registry for real-time progress reporting."""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any


STAGES = (
    "queued",
    "upload",
    "parse",
    "glossary",
    "awaiting_review",
    "translate",
    "docx",
    "pdf",
    "done",
    "error",
)


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
    # Glossary preview exposed to the UI while the job is paused at
    # awaiting_review. Once translation resumes this is left in place so the
    # final result can still surface it.
    glossary: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["elapsed"] = (self.finished_at or time.time()) - self.started_at
        return d


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._lock = threading.Lock()
        # Per-job rendezvous for the 2-Pass glossary review handshake.
        self._review_events: dict[str, threading.Event] = {}
        self._review_payload: dict[str, dict[str, str]] = {}

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

    def await_glossary_review(self, job_id: str, mapping: dict[str, str]) -> dict[str, str]:
        """Pause the pipeline thread until the user confirms the glossary.

        Publishes the extracted mapping on JobState (so the UI can render it),
        flips stage to ``awaiting_review``, and blocks on a per-job Event.
        Returns the user-edited mapping once resume_glossary_review is called.
        Falls back to the original mapping if the job is cleared mid-wait.
        """
        event = threading.Event()
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return mapping
            state.stage = "awaiting_review"
            state.processed = 0
            state.total = 0
            state.message = "용어집을 검토하고 '번역 시작' 버튼을 눌러주세요"
            state.glossary = dict(mapping)
            self._review_events[job_id] = event
            self._review_payload[job_id] = dict(mapping)
        event.wait()
        with self._lock:
            edited = self._review_payload.pop(job_id, mapping)
            self._review_events.pop(job_id, None)
            state = self._jobs.get(job_id)
            if state is not None:
                state.glossary = dict(edited)
        return edited

    def resume_glossary_review(self, job_id: str, edited: dict[str, str]) -> bool:
        """Hand the edited glossary back to the waiting pipeline thread."""
        with self._lock:
            event = self._review_events.get(job_id)
            if event is None:
                return False
            self._review_payload[job_id] = dict(edited)
        event.set()
        return True

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
            # Unblock any waiting review thread so it can exit cleanly.
            event = self._review_events.pop(job_id, None)
            self._review_payload.pop(job_id, None)
        if event is not None:
            event.set()


REGISTRY = JobRegistry()
