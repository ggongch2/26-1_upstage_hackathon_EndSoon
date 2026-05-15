"""Build a terminology glossary so chapter-wide translations stay consistent.

전체 텍스트를 Solar에 넘겨 핵심 전문 용어(영어 원문 → 권장 한국어 번역)를
JSON으로 받는다. 긴 문서는 청크로 나눠 추출 후 병합하며, 개별 청크가
타임아웃되어도 다른 청크 결과로 진행한다.
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from .solar import SolarClient

ProgressCb = Callable[[int, int], None]

log = logging.getLogger(__name__)

GLOSSARY_SYSTEM = (
    "You are a domain-aware terminology extractor for STEM textbooks. "
    "Given English passages from a college-level textbook, extract recurring "
    "technical terms and propose the most natural Korean translation. "
    "Return STRICT JSON: an object whose keys are the English source term "
    "(exact casing) and whose values are the recommended Korean rendering. "
    "Skip generic English words. Prefer Korean translations widely used in "
    "Korean academic textbooks. Limit to at most 60 entries per call."
)

GLOSSARY_USER_TMPL = (
    "Extract terminology mappings from this passage. JSON only, no prose.\n\n---\n{chunk}\n---"
)


@dataclass
class Glossary:
    mapping: dict[str, str]

    def as_prompt_block(self) -> str:
        """Inject the glossary as a JSON hard-constraint block.

        Empirically the model adheres to a structured JSON mapping much more
        reliably than a bulleted '- en → ko' list — the latter blends into
        prose and is easy for the model to gloss over.
        """
        if not self.mapping:
            return ""
        return (
            "Use the following terminology mapping as a HARD CONSTRAINT. "
            "Whenever a source term (key) appears in the input, the Korean "
            "output MUST use the mapped value verbatim — never paraphrase, "
            "translate differently, or omit the mapping.\n"
            "<terminology>\n"
            f"{json.dumps(self.mapping, ensure_ascii=False, indent=2)}\n"
            "</terminology>"
        )

    def apply(self, text: str) -> str:
        """Optional post-process: enforce glossary terms after model output."""
        if not self.mapping:
            return text
        out = text
        for en, ko in self.mapping.items():
            pattern = re.compile(rf"\b{re.escape(en)}\b", flags=re.IGNORECASE)
            out = pattern.sub(ko, out)
        return out


def _chunk(text: str, size: int = 6000) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks = []
    for i in range(0, len(text), size):
        chunks.append(text[i : i + size])
    return chunks


def _parse_json_loose(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    return {str(k).strip(): str(v).strip() for k, v in obj.items() if k and v}


def _glossary_workers(default: int = 4) -> int:
    try:
        return max(1, int(os.environ.get("GLOSSARY_WORKERS", str(default))))
    except ValueError:
        return default


def _glossary_timeout(default: float = 90.0) -> float:
    """Per-chunk Solar timeout. The pipeline-wide SolarClient default is 300s
    so a single slow chunk can stall the entire glossary stage; for glossary
    work we'd rather time out fast and miss a few terms than block."""
    try:
        return float(os.environ.get("GLOSSARY_TIMEOUT", str(default)))
    except ValueError:
        return default


def _extract_chunk(solar: SolarClient, chunk: str) -> dict[str, str]:
    """Run one Solar call for a single chunk and parse the JSON response.

    Raises on transport failure; the caller decides whether to swallow the
    error and continue with other chunks. Capped output tokens + tightened
    per-call timeout keep one runaway response from holding up the batch.
    """
    content = solar.chat(
        messages=[
            {"role": "system", "content": GLOSSARY_SYSTEM},
            {"role": "user", "content": GLOSSARY_USER_TMPL.format(chunk=chunk)},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
        max_tokens=1500,
        timeout=_glossary_timeout(),
    )
    return _parse_json_loose(content)


def build_glossary(
    solar: SolarClient,
    full_text: str,
    *,
    chunk_size: int | None = None,
    max_chunks: int | None = None,
    workers: int | None = None,
    on_progress: ProgressCb | None = None,
) -> Glossary:
    """Extract a terminology glossary from `full_text`.

    Chunks are dispatched in parallel via ThreadPoolExecutor; results are
    merged in original (document) order so the earliest occurrence of a
    term wins on conflicts — keeping behavior consistent with the previous
    sequential implementation.

    Tunables (env vars or kwargs):
      GLOSSARY_CHUNK_SIZE  default 6000  (chars per chunk)
      GLOSSARY_MAX_CHUNKS  default 3     (cap on chunks to scan)
      GLOSSARY_WORKERS     default 4     (parallel chunk calls)
    """
    if chunk_size is None:
        try:
            chunk_size = int(os.environ.get("GLOSSARY_CHUNK_SIZE", "6000"))
        except ValueError:
            chunk_size = 6000
    if max_chunks is None:
        try:
            max_chunks = int(os.environ.get("GLOSSARY_MAX_CHUNKS", "3"))
        except ValueError:
            max_chunks = 3
    if workers is None:
        workers = _glossary_workers()

    chunks = _chunk(full_text, size=chunk_size)[:max_chunks]
    total = len(chunks)
    if on_progress:
        on_progress(0, total)
    if not chunks:
        return Glossary(mapping={})

    # Single-chunk fast path — skip the thread pool overhead entirely.
    if total == 1 or workers <= 1:
        merged: dict[str, str] = {}
        for i, chunk in enumerate(chunks, 1):
            try:
                chunk_map = _extract_chunk(solar, chunk)
            except Exception as e:
                log.warning("glossary chunk %d/%d failed (%s); continuing", i, total, e)
                chunk_map = {}
            for k, v in chunk_map.items():
                merged.setdefault(k, v)
            log.info("glossary chunk %d/%d :: +%d terms (total=%d)", i, total, len(chunk_map), len(merged))
            if on_progress:
                on_progress(i, total)
        return Glossary(mapping=merged)

    # Parallel path. We collect results into a chunk-indexed dict so that
    # the merge step can iterate in document order even though futures
    # complete out of order.
    results: list[dict[str, str] | None] = [None] * total
    log.info("glossary: dispatching %d chunks across %d workers", total, workers)

    completed = 0
    with ThreadPoolExecutor(max_workers=min(workers, total)) as ex:
        futures = {ex.submit(_extract_chunk, solar, c): idx for idx, c in enumerate(chunks)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as e:
                log.warning("glossary chunk %d/%d failed (%s); continuing", idx + 1, total, e)
                results[idx] = {}
            completed += 1
            log.info(
                "glossary chunk %d/%d :: +%d terms",
                idx + 1, total, len(results[idx] or {}),
            )
            if on_progress:
                on_progress(completed, total)

    merged = {}
    for chunk_map in results:
        if not chunk_map:
            continue
        for k, v in chunk_map.items():
            merged.setdefault(k, v)
    log.info("glossary: merged total=%d terms from %d chunks", len(merged), total)
    return Glossary(mapping=merged)
