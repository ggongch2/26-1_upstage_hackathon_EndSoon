"""Element-aware translation with structure preservation.

원칙:
- 일반 텍스트 → 자연스러운 한국어
- 수식(equation) → LaTeX 그대로 유지 (텍스트 주변 설명만 번역 — 여기선 통째 보존)
- 표(table) → HTML 태그/속성/구조는 그대로, 내부 텍스트만 번역
- 그림(figure)/차트(chart) → 캡션 텍스트가 있으면 번역, 이미지 자체는 보존
- 용어집을 system 프롬프트에 박아 일관된 용어 사용
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from bs4 import BeautifulSoup, NavigableString

from .glossary import Glossary
from .parser import Element
from .solar import SolarClient

log = logging.getLogger(__name__)

SKIP_CATEGORIES = {"header", "footer", "footnote", "page_number"}
PASSTHROUGH_CATEGORIES = {"equation"}
TABLE_CATEGORIES = {"table"}

TRANSLATE_SYSTEM = (
    "You are a precise English→Korean translator for STEM university textbooks. "
    "Translate the user's text into natural, academic Korean. "
    "Rules:\n"
    "1. Preserve any LaTeX delimited by $...$, $$...$$, \\(...\\), or \\[...\\] EXACTLY.\n"
    "2. Preserve inline code, identifiers, numbers, units, and equation references verbatim.\n"
    "3. Do NOT add commentary, prefacing, or explanations — output ONLY the translation.\n"
    "4. Honor the supplied terminology mapping strictly.\n"
)


@dataclass
class TranslatedElement:
    element: Element
    translated_text: str
    translated_html: str


def _user_prompt(glossary: Glossary, payload: str) -> list[dict[str, str]]:
    glossary_block = glossary.as_prompt_block()
    parts = []
    if glossary_block:
        parts.append(glossary_block)
    parts.append("Translate the following to Korean. Output translation only.")
    parts.append("---")
    parts.append(payload)
    parts.append("---")
    return [
        {"role": "system", "content": TRANSLATE_SYSTEM},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _translate_text(solar: SolarClient, glossary: Glossary, text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    out = solar.chat(messages=_user_prompt(glossary, text), temperature=0.2)
    out = out.strip()
    out = re.sub(r"^---\s*", "", out)
    out = re.sub(r"\s*---$", "", out)
    return glossary.apply(out)


def _translate_table_html(solar: SolarClient, glossary: Glossary, html: str) -> str:
    """Walk the HTML tree and translate only NavigableString text nodes."""
    if not html.strip():
        return html
    soup = BeautifulSoup(html, "lxml")
    targets: list[NavigableString] = []
    for node in soup.descendants:
        if isinstance(node, NavigableString):
            s = str(node)
            if s.strip() and not s.strip().isdigit():
                targets.append(node)

    for node in targets:
        original = str(node)
        translated = _translate_text(solar, glossary, original)
        if translated:
            node.replace_with(translated)

    body = soup.body
    if body is not None:
        return body.decode_contents()
    return str(soup)


def translate_elements(
    solar: SolarClient,
    glossary: Glossary,
    elements: Iterable[Element],
) -> list[TranslatedElement]:
    results: list[TranslatedElement] = []
    for elem in elements:
        cat = elem.category.lower()
        if cat in SKIP_CATEGORIES:
            results.append(TranslatedElement(elem, elem.text, elem.html))
            continue
        if cat in PASSTHROUGH_CATEGORIES:
            results.append(TranslatedElement(elem, elem.text, elem.html))
            continue
        if cat in TABLE_CATEGORIES:
            try:
                new_html = _translate_table_html(solar, glossary, elem.html)
            except Exception:
                log.exception("table translate failed; keeping original")
                new_html = elem.html
            results.append(TranslatedElement(elem, elem.text, new_html))
            continue

        try:
            translated = _translate_text(solar, glossary, elem.text or elem.markdown)
        except Exception:
            log.exception("text translate failed; keeping original")
            translated = elem.text
        results.append(TranslatedElement(elem, translated, elem.html))
    return results
