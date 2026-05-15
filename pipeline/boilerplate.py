"""Detect running-header / footer style boilerplate that Document Parse
mis-classified as ``paragraph``.

Strategy: short text (≤ 80 chars) whose normalized form appears on 3+ distinct
pages is almost certainly the running header/footer band of the book (chapter
title, copyright line, section title repeated at the top of each page).
Those elements get marked SKIP so the translator never spends an API call on
them and the docx output stays clean.

This is intentionally simple — coordinate-aware detection (same y-position) is
better in theory but Document Parse coordinates are noisy enough that text
matching is more reliable in practice.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from .parser import Element


# Categories worth scanning for boilerplate. We scan headings too because
# some PDFs ship the running header as a heading-level element.
_SCAN_CATEGORIES = {
    "paragraph", "heading1", "heading2", "heading3", "title",
    "header", "footer",
}

_WS_RE = re.compile(r"\s+")
_EDGE_NUM_RE = re.compile(r"^\d+\s+|\s+\d+$")


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace + strip leading/trailing page numbers."""
    s = _WS_RE.sub(" ", text.lower()).strip()
    # Running headers often look like '198 LINEAR ALGEBRA ...' or
    # 'CHAPTER 4 ... 199' — strip the page number on either end so the
    # underlying title text matches across pages.
    prev = None
    while prev != s:
        prev = s
        s = _EDGE_NUM_RE.sub("", s).strip()
    return s


@dataclass
class BoilerplateHit:
    sample: str          # original text of the first occurrence
    normalized: str
    pages: list[int]     # sorted unique pages it appeared on
    element_ids: list[int]


def detect(
    elements: Iterable[Element],
    *,
    min_pages: int = 3,
    max_chars: int = 80,
    min_norm_chars: int = 4,
) -> tuple[set[int], list[BoilerplateHit]]:
    """Return (set of element ids to SKIP, list of hits with metadata).

    An element is flagged when its normalized text shows up on at least
    ``min_pages`` distinct pages. Long passages are exempted because a real
    paragraph repeating verbatim across pages is rare and risky to drop.
    """
    page_map: dict[str, set[int]] = defaultdict(set)
    id_map: dict[str, list[int]] = defaultdict(list)
    sample_map: dict[str, str] = {}

    for elem in elements:
        text = (elem.text or "").strip()
        if not text or len(text) > max_chars:
            continue
        if elem.category.lower() not in _SCAN_CATEGORIES:
            continue
        norm = _normalize(text)
        if len(norm) < min_norm_chars:
            continue
        page_map[norm].add(elem.page)
        id_map[norm].append(elem.id)
        sample_map.setdefault(norm, text)

    skip_ids: set[int] = set()
    hits: list[BoilerplateHit] = []
    for norm, pages in page_map.items():
        if len(pages) < min_pages:
            continue
        skip_ids.update(id_map[norm])
        hits.append(BoilerplateHit(
            sample=sample_map[norm],
            normalized=norm,
            pages=sorted(pages),
            element_ids=sorted(id_map[norm]),
        ))
    hits.sort(key=lambda h: (-len(h.pages), h.sample))
    return skip_ids, hits
