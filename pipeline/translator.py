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
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterable

ProgressCb = Callable[[int, int], None]

from bs4 import BeautifulSoup, NavigableString

from .glossary import Glossary
from .parser import Element
from .solar import SolarClient

log = logging.getLogger(__name__)

SKIP_CATEGORIES = {"header", "footer", "footnote", "page_number"}
PASSTHROUGH_CATEGORIES = {"equation", "figure", "chart"}
TABLE_CATEGORIES = {"table"}

TRANSLATE_SYSTEM = (
    "You are a precise English→Korean translator for STEM university textbooks.\n\n"
    "ABSOLUTE RULES — violating any of these makes the output unusable:\n"
    "1. Output EXACTLY ONE translation. NEVER multiple drafts, variants, alternatives, "
    "or 'final versions'. Pick the single best Korean rendering and stop.\n"
    "2. NO commentary, NO meta-explanations, NO translator notes, NO markdown headings "
    "like '**최종 번역:**' or '**다음 번역:**'. Output the translation as plain prose only.\n"
    "3. NO horizontal rules ('---'), NO bullet lists explaining choices, NO parenthetical "
    "asides discussing terminology decisions. Just the translation.\n"
    "4. Preserve any LaTeX (\\$...\\$, \\$\\$...\\$\\$, \\\\(...\\\\), \\\\[...\\\\]) and placeholders "
    "like ⟦M0⟧ EXACTLY — byte for byte.\n"
    "5. Preserve inline code, identifiers, numbers, units, and equation references verbatim.\n"
    "6. Honor the supplied terminology mapping strictly.\n"
    "7. DO NOT add markdown bold (**term**). The source is plain prose; emphasis is "
    "NOT to be invented. Output plain text only.\n"
    "8. STAY CLOSE TO SOURCE LENGTH. Output should be roughly the same length as the input. "
    "DO NOT elaborate, expand, add background context, paraphrase generously, or invent "
    "content. If the source is broken OCR or one short phrase, translate literally — "
    "never substitute your own elaboration.\n\n"
    "If the input is short, the output is short. If the input is one sentence, the output "
    "is one sentence."
)


@dataclass
class TranslatedElement:
    element: Element
    translated_text: str
    translated_html: str


def _user_prompt(
    glossary: Glossary,
    payload: str,
    *,
    prev_context: str = "",
    next_context: str = "",
) -> list[dict[str, str]]:
    glossary_block = glossary.as_prompt_block()
    parts = []
    if glossary_block:
        parts.append(glossary_block)
    if prev_context or next_context:
        parts.append(
            "CONTEXT (surrounding text from the same document — for reference ONLY, "
            "DO NOT translate or include any of this in your output):"
        )
        if prev_context:
            parts.append(f"[Previous text]\n{prev_context}")
        if next_context:
            parts.append(f"[Next text]\n{next_context}")
    parts.append("TRANSLATE ONLY THE TEXT BETWEEN THE FENCES BELOW. Output translation only.")
    parts.append("---")
    parts.append(payload)
    parts.append("---")
    return [
        {"role": "system", "content": TRANSLATE_SYSTEM},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


_LATEX_PATTERNS = [
    (re.compile(r"\$\$[^$]+?\$\$", re.DOTALL), "display_dollar"),
    (re.compile(r"\\\[[\s\S]+?\\\]"), "display_bracket"),
    (re.compile(r"\$[^$\n]+?\$"), "inline_dollar"),
    (re.compile(r"\\\([\s\S]+?\\\)"), "inline_paren"),
]


def _protect_latex(text: str) -> tuple[str, list[str]]:
    """Replace LaTeX runs with opaque placeholders so the LLM cannot mangle them."""
    saved: list[str] = []

    def _sub(m: re.Match) -> str:
        saved.append(m.group(0))
        return f"⟦M{len(saved)-1}⟧"

    for pattern, _name in _LATEX_PATTERNS:
        text = pattern.sub(_sub, text)
    return text, saved


def _restore_latex(text: str, saved: list[str]) -> str:
    for i, original in enumerate(saved):
        text = text.replace(f"⟦M{i}⟧", original)
    return text


def _translate_text(
    solar: SolarClient,
    glossary: Glossary,
    text: str,
    *,
    prev_context: str = "",
    next_context: str = "",
) -> str:
    text = text.strip()
    if not text:
        return ""
    protected, saved = _protect_latex(text)
    # Hard cap output length proportional to input — prevents the model from
    # writing a multi-paragraph elaboration when the source is a single phrase.
    # Korean is ~1 token/char so this gives a generous ~3x buffer.
    max_tokens = max(120, len(protected) * 3)
    out = solar.chat(
        messages=_user_prompt(
            glossary, protected,
            prev_context=prev_context.strip()[:600],
            next_context=next_context.strip()[:600],
        ),
        temperature=0.0,
        max_tokens=max_tokens,
    )
    out = _clean_model_output(out)
    out = _restore_latex(out, saved)
    return glossary.apply(out)


_HR_RE = re.compile(r"(?m)^\s*-{3,}\s*$")
# A markdown bold header like '**번역:**', '**수정 요청 사항:**'. When this appears
# anywhere in the answer, the model has switched from translating to commentary and
# everything from that point on is unreliable.
# Matches headers like '**번역:**', '**수정 요청 사항:**' where the colon lives
# INSIDE the bold delimiters. Also matches the variant '**번역**:' with colon outside.
_META_HEADER_RE = re.compile(
    r"\*\*\s*[^\n*]{1,40}?[:：]\s*\*\*"
    r"|\*\*\s*[^\n*]{1,40}?\s*\*\*\s*[:：]"
)


def _clean_model_output(out: str) -> str:
    """Strip Solar's meta-commentary. Strategy: locate the first markdown section-header
    of the form '**...:**' and truncate the response there — the prose before it is
    almost always the actual translation, everything after is rambling.

    Also strips remaining '**term**' bold markers (the model loves to emphasize every
    technical term, which is not how textbook prose reads)."""
    out = out.strip()
    out = re.sub(r"^---\s*", "", out)
    out = re.sub(r"\s*---$", "", out)
    out = _HR_RE.sub("", out)
    m = _META_HEADER_RE.search(out)
    if m:
        out = out[: m.start()].rstrip()
    parts = re.split(r"\n\s*\n", out)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > 1:
        parts = [
            p for p in parts
            if not (p.startswith("(") and p.endswith(")") and ("번역" in p or "translation" in p.lower()))
        ]
        out = "\n\n".join(parts) if parts else out
    # Strip remaining '**term**' bold abuse — keep content, drop the markers.
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", out)
    return out.strip()


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


CONTEXT_USABLE = {"paragraph", "heading1", "heading2", "heading3", "title", "list", "caption"}


def _looks_like_ocr_garbage(text: str) -> bool:
    """Heuristic: True if the paragraph is mostly OCR residue of a matrix or table
    (lots of pipes / numbers / brackets, few real words). Such elements are not
    meaningfully translatable and just clutter the output.

    Rules (text must be ≥ 12 chars):
    1. Pipe density: 3+ pipes AND pipes/length > 8%
    2. Letter density: alpha+Hangul chars / total < 30%
    """
    s = (text or "").strip()
    n = len(s)
    if n < 12:
        return False
    pipes = s.count("|")
    if pipes >= 3 and pipes / n > 0.08:
        return True
    letters = sum(1 for c in s if c.isalpha())
    if letters / n < 0.30:
        return True
    return False


def _translate_one(
    solar: SolarClient,
    glossary: Glossary,
    elem: Element,
    *,
    prev_text: str = "",
    next_text: str = "",
) -> TranslatedElement:
    cat = elem.category.lower()
    if cat in SKIP_CATEGORIES or cat in PASSTHROUGH_CATEGORIES:
        return TranslatedElement(elem, elem.text, elem.html)
    if cat in TABLE_CATEGORIES:
        try:
            new_html = _translate_table_html(solar, glossary, elem.html)
        except Exception:
            log.exception("table translate failed; keeping original")
            new_html = elem.html
        return TranslatedElement(elem, elem.text, new_html)

    src = elem.text or elem.markdown
    # Drop matrix/table OCR residue — saves an API call AND avoids ugly pipe
    # garbage in the output. The empty translated_text causes docx_builder to
    # skip the paragraph entirely.
    if _looks_like_ocr_garbage(src):
        log.info("translator: skipping OCR-garbage element id=%s page=%s text=%r",
                 elem.id, elem.page, src[:60])
        return TranslatedElement(elem, "", elem.html)

    try:
        translated = _translate_text(
            solar, glossary, src,
            prev_context=prev_text, next_context=next_text,
        )
    except Exception:
        log.exception("text translate failed; keeping original")
        translated = elem.text
    return TranslatedElement(elem, translated, elem.html)


def _build_neighbor_index(items: list[Element]) -> tuple[list[str], list[str]]:
    """For each element, find the nearest preceding and following element whose
    category is in CONTEXT_USABLE, and return their text. Headers/footers/figures
    are skipped — they're not useful as translation context."""
    prevs: list[str] = []
    nexts: list[str] = []
    for i in range(len(items)):
        prev_text = ""
        for j in range(i - 1, max(-1, i - 6), -1):
            if items[j].category.lower() in CONTEXT_USABLE and items[j].text.strip():
                prev_text = items[j].text.strip()
                break
        next_text = ""
        for j in range(i + 1, min(len(items), i + 6)):
            if items[j].category.lower() in CONTEXT_USABLE and items[j].text.strip():
                next_text = items[j].text.strip()
                break
        prevs.append(prev_text)
        nexts.append(next_text)
    return prevs, nexts


def translate_elements(
    solar: SolarClient,
    glossary: Glossary,
    elements: Iterable[Element],
    *,
    on_progress: ProgressCb | None = None,
) -> list[TranslatedElement]:
    items = list(elements)
    total = len(items)
    try:
        workers = max(1, int(os.environ.get("TRANSLATE_WORKERS", "5")))
    except ValueError:
        workers = 5

    prevs, nexts = _build_neighbor_index(items)

    if on_progress:
        on_progress(0, total)

    if workers == 1 or total <= 1:
        out: list[TranslatedElement] = []
        for i, e in enumerate(items, 1):
            out.append(_translate_one(solar, glossary, e, prev_text=prevs[i-1], next_text=nexts[i-1]))
            if on_progress:
                on_progress(i, total)
        return out

    results: list[TranslatedElement | None] = [None] * total
    log_every = max(1, total // 20)  # ~5% increments
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _translate_one, solar, glossary, elem,
                prev_text=prevs[idx], next_text=nexts[idx],
            ): idx
            for idx, elem in enumerate(items)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception:
                log.exception("worker failed for element %d; keeping original", idx)
                e = items[idx]
                results[idx] = TranslatedElement(e, e.text, e.html)
            completed += 1
            if on_progress:
                on_progress(completed, total)
            if completed % log_every == 0 or completed == total:
                log.info("translated %d/%d elements (workers=%d)", completed, total, workers)

    return [r for r in results if r is not None]
