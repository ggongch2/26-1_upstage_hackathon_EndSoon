"""Element-aware translation with structure preservation.

라우팅 (현재 동작):
- paragraph/heading/list/caption → Solar 번역 (LaTeX placeholder 보호, context 전달)
- header → 페이지 번호면 drop, 챕터 헤더면 작은 italic 번역
- table → main.py에서 PDF 영역 크롭한 base64가 붙어 있으면 이미지로 통과 (번역 X).
  base64 없을 때만 HTML 트리 walk로 셀 텍스트 번역 (fallback).
- equation → main.py에서 PDF 크롭 base64가 붙어 있으면 이미지로 통과.
  없을 때만 docx_builder에서 LaTeX → MathML → OMML로 렌더 (fallback).
- figure/chart → 항상 PASSTHROUGH. Upstage Document Parse base64 그대로.
- header(페이지번호)/footer/footnote → SKIP.

번역 보호 장치:
1. _wrap_unicode_math: U+1D400-U+1D7FF 영역 unicode bold-italic 글자를 \\mathbf{} LaTeX로 변환.
2. _protect_latex: \\$..\\$, \\(..\\), \\[..\\] 패턴을 ⟦M0⟧ placeholder로 치환해 모델이 못 만짐.
3. _looks_like_ocr_garbage: 파이프·문자 밀도 휴리스틱으로 깨진 행렬 잔해 element 자동 폐기.
4. _detect_near_duplicates: 80-char fingerprint로 Document Parse가 같은 단락 두 번 뱉은 케이스 dedup.
5. _clean_model_output: '**번역:**', '[정확한 번역]', 'However, to strictly...' 등 메타 코멘트
   첫 매치 위치에서 truncation. 남은 **bold** 마커도 strip.
6. Glossary: chapter 전체에서 추출한 용어집을 system prompt에 강제 주입.

호출 패턴:
- _translate_one은 ThreadPoolExecutor 워커당 1번 실행.
- TRANSLATE_WORKERS 환경변수로 동시 처리 개수 조정.
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

from . import boilerplate as boilerplate_mod
from .glossary import Glossary
from .parser import Element
from .solar import SolarClient

log = logging.getLogger(__name__)

SKIP_CATEGORIES = {"footer", "footnote", "page_number"}
PASSTHROUGH_CATEGORIES = {"equation", "figure", "chart"}
TABLE_CATEGORIES = {"table"}
# Categories eligible to be grouped into a marker-batched page payload.
GROUPABLE_CATEGORIES = {
    "paragraph", "heading1", "heading2", "heading3", "heading4",
    "title", "list", "caption",
}
# Max characters of source text packed into a single page-batch request.
# Above this we split into multiple chunks so a single Solar call stays
# well under the model's context budget.
GROUP_MAX_CHARS = 3500
# 'header' is special: pure-number page headers (e.g. "198") are dropped,
# but running chapter headers ("4.1 Two Pictures of Linear Equations") are translated.

# Two distinct system prompts — single-element and marker-batched. We deliberately
# do NOT have the batch prompt inherit from the single one: the single prompt's
# "exactly one translation / plain Korean prose only" rule directly conflicts with
# batched output (multiple marker blocks). Keeping them separate avoids the model
# guessing which regime applies.

_COMMON_PRESERVE = (
    "Preserve LaTeX ($...$, $$...$$, \\(...\\), \\[...\\]), MathML, placeholders "
    "like ⟦M0⟧, inline code, identifiers, numbers, units, and equation references "
    "EXACTLY — byte for byte.\n"
    "Do NOT translate variable names, matrix entries, symbolic expressions, or "
    "formula structure. Treat them as preserve-only tokens.\n"
)
_COMMON_FRAGMENT = (
    "If the source is a fragment (heading, caption, list item, table cell, short "
    "phrase), output a fragment of the same kind — not a full explanatory sentence. "
    "Match source length and structure; do not elaborate.\n"
    "For table cells, translate only natural-language text; preserve formulas, "
    "symbols, abbreviations, and compact formatting as-is.\n"
    "If the source is broken OCR or malformed math, translate as literally as "
    "possible — never guess or substitute your own content.\n"
)
_COMMON_NO_META = (
    "Do NOT add commentary, meta-explanations, translator notes, labels (e.g. "
    "'**번역:**', '[정확한 번역]', '최종 번역'), horizontal rules ('---'), "
    "markdown bold (**term**), or parenthetical asides — in any language.\n"
)

TRANSLATE_SYSTEM_SINGLE = (
    "You are a precise English-to-Korean translator for STEM university textbooks.\n\n"
    "Your task is to translate ONLY the text provided inside <source>...</source> "
    "into natural Korean.\n\n"
    "Rules:\n"
    "1. Output exactly one Korean translation and nothing else — no surrounding tags.\n"
    "2. " + _COMMON_NO_META
    + "3. " + _COMMON_PRESERVE
    + "4. " + _COMMON_FRAGMENT
    + "5. Honor the supplied terminology mapping as a hard constraint.\n"
    "6. Output plain Korean text only."
)

TRANSLATE_SYSTEM_BATCH = (
    "You are a precise English-to-Korean translator for STEM university textbooks.\n\n"
    "The input inside <segments>...</segments> contains multiple text blocks "
    "wrapped in markers ⟦Ek⟧...⟦/Ek⟧. Translate the content inside each marker "
    "block into Korean.\n\n"
    "Rules:\n"
    "1. Return the SAME markers in the SAME order — do not merge, split, skip, "
    "reorder, or invent blocks.\n"
    "2. Output nothing outside the marker blocks (no preamble, no trailing notes).\n"
    "3. Inside each block, output exactly one Korean translation of that block.\n"
    "4. " + _COMMON_PRESERVE
    + "5. " + _COMMON_FRAGMENT
    + "6. Honor the supplied terminology mapping as a hard constraint.\n"
    "7. " + _COMMON_NO_META.rstrip()
)

# Backwards-compat alias — some external callers may still reference the
# original name. Kept as the SINGLE variant since that matches its historical
# semantics (element-by-element translation).
TRANSLATE_SYSTEM = TRANSLATE_SYSTEM_SINGLE


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
            parts.append(f"<previous>\n{prev_context}\n</previous>")
        if next_context:
            parts.append(f"<next>\n{next_context}\n</next>")
    parts.append("Translate only the text inside <source>. Do not translate anything outside it.")
    parts.append("<source>")
    parts.append(payload)
    parts.append("</source>")
    return [
        {"role": "system", "content": TRANSLATE_SYSTEM_SINGLE},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


_LATEX_PATTERNS = [
    (re.compile(r"\$\$[^$]+?\$\$", re.DOTALL), "display_dollar"),
    (re.compile(r"\\\[[\s\S]+?\\\]"), "display_bracket"),
    (re.compile(r"\$[^$\n]+?\$"), "inline_dollar"),
    (re.compile(r"\\\([\s\S]+?\\\)"), "inline_paren"),
]


# Unicode Mathematical Alphanumeric Symbols ranges (U+1D400 - U+1D7FF).
# Textbook PDFs ship bold/italic letters as these code points; without
# conversion they pass through as raw glyphs that 맑은 고딕 cannot render,
# showing up as boxes or fallback chars. Wrap each run in \(...\) so the
# OMML pipeline picks them up as real math objects.

def _math_style_command(cp: int) -> str | None:
    if 0x1D400 <= cp <= 0x1D433:                                 # Bold
        return "mathbf"
    if 0x1D434 <= cp <= 0x1D467 or cp == 0x210E:                 # Italic
        return None
    if 0x1D468 <= cp <= 0x1D49B:                                 # Bold italic
        return "mathbf"
    if 0x1D49C <= cp <= 0x1D503:                                 # Script / bold script
        return "mathcal"
    if 0x1D504 <= cp <= 0x1D537 or 0x1D56C <= cp <= 0x1D59F:     # Fraktur / bold fraktur
        return "mathfrak"
    if 0x1D538 <= cp <= 0x1D56B:                                 # Double-struck
        return "mathbb"
    if 0x1D5A0 <= cp <= 0x1D607:                                 # Sans-serif (+ bold)
        return "mathsf"
    if 0x1D670 <= cp <= 0x1D6A3:                                 # Monospace
        return "mathtt"
    if 0x1D7CE <= cp <= 0x1D7D7 or 0x1D7EC <= cp <= 0x1D7FF:     # Bold digits / sans-bold digits
        return "mathbf"
    return None


def _wrap_unicode_math(text: str) -> str:
    if not text:
        return text
    import unicodedata as _ud

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        cp = ord(text[i])
        if 0x1D400 <= cp <= 0x1D7FF:
            j = i + 1
            while j < n and 0x1D400 <= ord(text[j]) <= 0x1D7FF:
                j += 1
            run = text[i:j]
            normalized = _ud.normalize("NFKC", run)
            style = _math_style_command(cp)
            if normalized.isascii() and any(c.isalpha() or c.isdigit() for c in normalized):
                if style:
                    out.append(f"\\(\\{style}{{{normalized}}}\\)")
                else:
                    out.append(f"\\({normalized}\\)")
            else:
                # Greek / other non-ASCII normalization — pass through normalized form.
                out.append(normalized)
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


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
    text = _wrap_unicode_math(text)
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
# Multiple meta-marker patterns. Each one signals "the model stopped translating
# and started commenting"; we truncate at the earliest hit.
_META_PATTERNS = [
    re.compile(r"\*\*\s*[^\n*]{1,40}?[:：]\s*\*\*"),                    # **번역:**
    re.compile(r"\*\*\s*[^\n*]{1,40}?\s*\*\*\s*[:：]"),                 # **번역**:
    re.compile(r"\[\s*(?:정확한|대안|최종|개선된|수정|두\s*번째|또\s*다른|참고|예시)\s*번역\s*\]"),
    re.compile(r"However,\s+to\s+strictly\s+adhere", re.IGNORECASE),
    re.compile(r"However,\s+the\s+translation", re.IGNORECASE),
    re.compile(r"This\s+preserves\s+the\s+exact", re.IGNORECASE),
    re.compile(r"To\s+strictly\s+adhere\s+to", re.IGNORECASE),
    re.compile(r"\(\s*※"),                                              # (※ ...)
]


def _find_first_meta(text: str) -> "re.Match[str] | None":
    earliest = None
    for r in _META_PATTERNS:
        m = r.search(text)
        if m and (earliest is None or m.start() < earliest.start()):
            earliest = m
    return earliest


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
    m = _find_first_meta(out)
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


_MARKER_OPEN = "⟦E{}⟧"
_MARKER_CLOSE = "⟦/E{}⟧"
_MARKER_RE = re.compile(r"⟦E(\d+)⟧\s*(.*?)\s*⟦/E\1⟧", re.DOTALL)


def _build_marked_payload(elems: list[Element]) -> tuple[str, list[Element]]:
    """Build the ⟦Ek⟧-wrapped payload. Returns (prompt_text, ordered_elements).

    Elements with empty source text are dropped from the batch.
    """
    parts: list[str] = []
    used: list[Element] = []
    for e in elems:
        text = (e.text or e.markdown or "").strip()
        if not text:
            continue
        text = _wrap_unicode_math(text)
        idx = len(used)
        parts.append(f"{_MARKER_OPEN.format(idx)}\n{text}\n{_MARKER_CLOSE.format(idx)}")
        used.append(e)
    return "\n".join(parts), used


def _parse_marked_response(out: str, count: int) -> list[str]:
    """Pull translations out of the marker-tagged response.

    Slots the model failed to emit come back as empty strings; the caller
    re-translates those individually as a fallback.
    """
    results = [""] * count
    for m in _MARKER_RE.finditer(out):
        k = int(m.group(1))
        if 0 <= k < count:
            results[k] = m.group(2).strip()
    return results


def _split_page_into_chunks(elems: list[Element], max_chars: int = GROUP_MAX_CHARS) -> list[list[Element]]:
    """Split a page's groupable elements into batches that fit within max_chars."""
    chunks: list[list[Element]] = []
    cur: list[Element] = []
    cur_size = 0
    for e in elems:
        size = len((e.text or "").strip())
        if cur and cur_size + size > max_chars:
            chunks.append(cur)
            cur = [e]
            cur_size = size
        else:
            cur.append(e)
            cur_size += size
    if cur:
        chunks.append(cur)
    return chunks


def _translate_page_chunk(
    solar: SolarClient,
    glossary: Glossary,
    chunk: list[Element],
    *,
    prev_context: str = "",
    next_context: str = "",
) -> dict[int, str]:
    """Translate a chunk of page elements with marker batching.

    Returns a dict {element_id: translated_text}. Elements the model failed
    to translate within the batch are re-tried one-by-one via _translate_text
    so a partial-batch failure doesn't lose data.
    """
    payload, used = _build_marked_payload(chunk)
    if not used:
        return {}

    glossary_block = glossary.as_prompt_block()
    parts: list[str] = []
    if glossary_block:
        parts.append(glossary_block)
    if prev_context or next_context:
        parts.append(
            "CONTEXT (surrounding text from the same document — for reference "
            "ONLY, DO NOT translate or include any of this in your output):"
        )
        if prev_context:
            parts.append(f"<previous>\n{prev_context}\n</previous>")
        if next_context:
            parts.append(f"<next>\n{next_context}\n</next>")
    parts.append(
        "Translate each ⟦Ek⟧...⟦/Ek⟧ block inside <segments>. Return the same "
        "markers in the same order. Output nothing outside the markers."
    )
    parts.append("<segments>")
    parts.append(payload)
    parts.append("</segments>")

    protected, saved = _protect_latex("\n\n".join(parts))
    messages = [
        {"role": "system", "content": TRANSLATE_SYSTEM_BATCH},
        {"role": "user", "content": protected},
    ]
    # Output budget: batched payload + Korean overhead. We give ~3x source
    # length, capped to keep one request from running away.
    src_chars = sum(len((e.text or "").strip()) for e in used)
    max_tokens = min(8000, max(400, src_chars * 3))

    try:
        out = solar.chat(messages=messages, temperature=0.0, max_tokens=max_tokens)
    except Exception:
        log.exception("page-batch call failed; falling back to per-element")
        return _translate_chunk_individually(solar, glossary, used)
    out = _restore_latex(out, saved)
    translated = _parse_marked_response(out, len(used))

    result: dict[int, str] = {}
    missing: list[Element] = []
    for elem, t in zip(used, translated):
        if not t:
            missing.append(elem)
            continue
        cleaned = _clean_model_output(t)
        if cleaned:
            result[elem.id] = glossary.apply(cleaned)
        else:
            missing.append(elem)

    if missing:
        log.info(
            "page-batch: %d/%d markers missing/empty — re-trying individually",
            len(missing), len(used),
        )
        result.update(_translate_chunk_individually(solar, glossary, missing))
    return result


def _translate_chunk_individually(
    solar: SolarClient, glossary: Glossary, elems: list[Element],
) -> dict[int, str]:
    out: dict[int, str] = {}
    for e in elems:
        try:
            out[e.id] = _translate_text(solar, glossary, e.text or "")
        except Exception:
            log.exception("per-element fallback failed id=%s", e.id)
            out[e.id] = e.text or ""
    return out


def _translate_one(
    solar: SolarClient,
    glossary: Glossary,
    elem: Element,
    *,
    prev_text: str = "",
    next_text: str = "",
    is_duplicate: bool = False,
) -> TranslatedElement:
    cat = elem.category.lower()
    if cat in SKIP_CATEGORIES or cat in PASSTHROUGH_CATEGORIES:
        return TranslatedElement(elem, elem.text, elem.html)
    if cat == "header":
        # Pure number = page number → drop; otherwise translate the running header.
        stripped = (elem.text or "").strip()
        if not stripped or stripped.replace(".", "").isdigit():
            return TranslatedElement(elem, "", elem.html)
        try:
            translated = _translate_text(solar, glossary, stripped)
        except Exception:
            log.exception("header translate failed; keeping original")
            translated = stripped
        return TranslatedElement(elem, translated, elem.html)
    if cat in TABLE_CATEGORIES:
        # If a PDF-cropped image is attached, skip translation entirely — the
        # docx will render the table as a faithful image of the original.
        if elem.base64:
            return TranslatedElement(elem, elem.text, elem.html)
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
    if is_duplicate:
        log.info("translator: skipping duplicate element id=%s page=%s", elem.id, elem.page)
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


def _detect_near_duplicates(items: list[Element]) -> set[int]:
    """Return set of element ids that are near-duplicates of an earlier element.

    Document Parse occasionally returns the same paragraph twice (once as text,
    once as part of a different region) — emit only the first occurrence.
    Uses normalized 12-token shingles to compare.
    """
    seen: dict[str, int] = {}
    dupes: set[int] = set()
    for elem in items:
        cat = elem.category.lower()
        if cat not in CONTEXT_USABLE:
            continue
        text = (elem.text or "").strip()
        if len(text) < 30:
            continue
        # Normalize: lowercase, strip non-alnum
        norm = re.sub(r"\W+", " ", text.lower()).strip()
        # Use the leading 80 chars as fingerprint
        fp = norm[:80]
        if fp in seen:
            dupes.add(elem.id)
        else:
            seen[fp] = elem.id
    return dupes


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
    """Translate elements with two layers of batching:

    1. Page-grouped text (paragraph/heading/list/caption) goes through the
       marker-batched _translate_page_chunk path — one Solar call per chunk
       of up to ~3500 chars. Cuts call count by ~10–30x.
    2. Special elements (table/equation/figure/header/...) still go through
       _translate_one element-by-element so structure-preserving logic
       (HTML walk, image passthrough, OCR-garbage filter) remains intact.

    Boilerplate (running headers/footers detected as repeated short text
    across 3+ pages) is skipped entirely with empty translated_text so the
    docx builder drops those paragraphs.
    """
    items = list(elements)
    total = len(items)
    try:
        workers = max(1, int(os.environ.get("TRANSLATE_WORKERS", "5")))
    except ValueError:
        workers = 5

    dupes = _detect_near_duplicates(items)
    if dupes:
        log.info("translator: %d near-duplicate elements will be dropped", len(dupes))

    skip_boiler, boiler_hits = boilerplate_mod.detect(items)
    for hit in boiler_hits:
        log.info(
            "boilerplate detected (%d pages, ids=%s): %r",
            len(hit.pages), hit.element_ids[:6], hit.sample,
        )
    if boiler_hits:
        log.info("translator: %d boilerplate elements will be dropped", len(skip_boiler))

    prevs, nexts = _build_neighbor_index(items)
    prev_by_id = {e.id: prevs[i] for i, e in enumerate(items)}
    next_by_id = {e.id: nexts[i] for i, e in enumerate(items)}

    # Pre-fill results for every element so the page-batch path and the
    # per-element path can both write into the same indexed slot.
    results: list[TranslatedElement | None] = [None] * total
    index_by_id: dict[int, int] = {e.id: i for i, e in enumerate(items)}
    completed = 0

    def _bump(n: int = 1) -> None:
        nonlocal completed
        completed += n
        if on_progress:
            on_progress(completed, total)

    # Stage A — handle boilerplate/dup/skip/passthrough/special items inline,
    # and collect groupable text elements per page for batched translation.
    page_groups: dict[int, list[Element]] = {}
    for idx, e in enumerate(items):
        cat = e.category.lower()

        if e.id in skip_boiler:
            # Drop entirely (empty text) — docx_builder skips empty paragraphs.
            results[idx] = TranslatedElement(e, "", e.html)
            _bump()
            continue

        if cat in GROUPABLE_CATEGORIES and not _looks_like_ocr_garbage(e.text or "") and e.id not in dupes:
            page_groups.setdefault(e.page, []).append(e)
            continue

        # Everything else (table, equation, figure, chart, header, footer,
        # OCR garbage, duplicates, footnote, page_number) goes through the
        # legacy per-element path. Fast because most of those are SKIP /
        # PASSTHROUGH and never hit the API.
        try:
            results[idx] = _translate_one(
                solar, glossary, e,
                prev_text=prev_by_id.get(e.id, ""),
                next_text=next_by_id.get(e.id, ""),
                is_duplicate=(e.id in dupes),
            )
        except Exception:
            log.exception("special-path failed for element id=%s", e.id)
            results[idx] = TranslatedElement(e, e.text, e.html)
        _bump()

    # Stage B — split each page's groupable elements into chunks and
    # translate in parallel.
    chunks: list[list[Element]] = []
    for page in sorted(page_groups):
        chunks.extend(_split_page_into_chunks(page_groups[page]))

    if on_progress:
        on_progress(completed, total)

    def _run_chunk(chunk: list[Element]) -> dict[int, str]:
        # Use the first and last elements of the chunk to look up context
        # — these were computed from the neighbor index above.
        prev_ctx = prev_by_id.get(chunk[0].id, "") if chunk else ""
        next_ctx = next_by_id.get(chunk[-1].id, "") if chunk else ""
        return _translate_page_chunk(
            solar, glossary, chunk,
            prev_context=prev_ctx, next_context=next_ctx,
        )

    log.info(
        "page-batch translation: %d chunks across %d pages (workers=%d)",
        len(chunks), len(page_groups), workers,
    )

    if workers == 1 or len(chunks) <= 1:
        for chunk in chunks:
            mapping = _run_chunk(chunk)
            for e in chunk:
                t = mapping.get(e.id, e.text or "")
                results[index_by_id[e.id]] = TranslatedElement(e, t, e.html)
            _bump(len(chunk))
    else:
        log_every = max(1, len(chunks) // 10)
        done_chunks = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_run_chunk, c): c for c in chunks}
            for fut in as_completed(futs):
                chunk = futs[fut]
                try:
                    mapping = fut.result()
                except Exception:
                    log.exception("page-batch worker failed; keeping originals")
                    mapping = {e.id: e.text or "" for e in chunk}
                for e in chunk:
                    t = mapping.get(e.id, e.text or "")
                    results[index_by_id[e.id]] = TranslatedElement(e, t, e.html)
                done_chunks += 1
                _bump(len(chunk))
                if done_chunks % log_every == 0 or done_chunks == len(chunks):
                    log.info("page-batch %d/%d chunks done", done_chunks, len(chunks))

    return [r for r in results if r is not None]
