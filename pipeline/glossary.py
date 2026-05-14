"""Build a terminology glossary so chapter-wide translations stay consistent.

전체 텍스트를 Solar에 넘겨 핵심 전문 용어(영어 원문 → 권장 한국어 번역)를
JSON으로 받는다. 긴 문서는 청크로 나눠 추출 후 병합하며, 개별 청크가
타임아웃되어도 다른 청크 결과로 진행한다.
"""
from __future__ import annotations

import json
import logging
import re
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
        if not self.mapping:
            return ""
        lines = [f"- {en} → {ko}" for en, ko in self.mapping.items()]
        return "Use this fixed terminology mapping for consistency:\n" + "\n".join(lines)

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


def build_glossary(
    solar: SolarClient,
    full_text: str,
    *,
    chunk_size: int = 6000,
    max_chunks: int = 3,
    on_progress: ProgressCb | None = None,
) -> Glossary:
    chunks = _chunk(full_text, size=chunk_size)[:max_chunks]
    total = len(chunks)
    merged: dict[str, str] = {}
    if on_progress:
        on_progress(0, total)
    for i, chunk in enumerate(chunks, 1):
        try:
            content = solar.chat(
                messages=[
                    {"role": "system", "content": GLOSSARY_SYSTEM},
                    {"role": "user", "content": GLOSSARY_USER_TMPL.format(chunk=chunk)},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            log.warning("glossary chunk %d/%d failed (%s); continuing", i, total, e)
            if on_progress:
                on_progress(i, total)
            continue
        chunk_map = _parse_json_loose(content)
        for k, v in chunk_map.items():
            merged.setdefault(k, v)
        log.info("glossary chunk %d/%d :: +%d terms (total=%d)", i, total, len(chunk_map), len(merged))
        if on_progress:
            on_progress(i, total)
    return Glossary(mapping=merged)
