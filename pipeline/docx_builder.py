"""Reassemble translated elements into a Word document preserving order/layout."""
from __future__ import annotations

import base64
import logging
import re
from io import BytesIO
from pathlib import Path

from bs4 import BeautifulSoup
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

from .translator import TranslatedElement

KOREAN_FONT = "맑은 고딕"

log = logging.getLogger(__name__)

HEADING_LEVELS = {
    "heading1": 1,
    "heading2": 2,
    "heading3": 3,
    "title": 0,
}

IMAGE_CATEGORIES = {"figure", "chart"}
TABLE_CATEGORIES = {"table"}


def _add_image_from_base64(doc: Document, b64: str, max_width_inches: float = 5.5) -> bool:
    if not b64:
        return False
    data = b64
    if "," in data:
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data)
    except Exception:
        log.warning("invalid base64 image; skipping")
        return False
    bio = BytesIO(raw)
    try:
        doc.add_picture(bio, width=Inches(max_width_inches))
        return True
    except Exception:
        log.exception("add_picture failed; skipping image")
        return False


def _add_html_table(doc: Document, html: str) -> None:
    if not html.strip():
        return
    soup = BeautifulSoup(html, "lxml")
    table_tag = soup.find("table")
    if not table_tag:
        doc.add_paragraph(soup.get_text("\n", strip=True))
        return

    rows = table_tag.find_all("tr")
    if not rows:
        return
    cols = max(len(r.find_all(["td", "th"])) for r in rows)
    if cols == 0:
        return

    table = doc.add_table(rows=len(rows), cols=cols)
    table.style = "Table Grid"
    for ri, tr in enumerate(rows):
        cells = tr.find_all(["td", "th"])
        for ci in range(cols):
            text = cells[ci].get_text(" ", strip=True) if ci < len(cells) else ""
            table.cell(ri, ci).text = text


_INLINE_MATH_RE = re.compile(
    r"(\$\$[^$]+?\$\$|\\\[[\s\S]+?\\\]|\$[^$\n]+?\$|\\\([\s\S]+?\\\))"
)


def _add_equation_paragraph(doc: Document, text: str) -> None:
    """Render LaTeX as a real Word equation (OMML) when possible; else as math-font text."""
    from .math_render import insert_math_into_paragraph

    p = doc.add_paragraph()
    if insert_math_into_paragraph(p, text):
        return
    run = p.add_run(text)
    run.font.name = "Cambria Math"
    run.font.size = Pt(12)
    run.italic = True


def _add_text_with_inline_math(doc: Document, text: str, *, style: str | None = None) -> None:
    r"""Add a paragraph, splitting any inline LaTeX (\$...\$, \(...\), etc.) into real OMML runs."""
    from .math_render import insert_math_into_paragraph

    parts = _INLINE_MATH_RE.split(text)
    if len(parts) == 1:
        if style:
            doc.add_paragraph(text, style=style)
        else:
            doc.add_paragraph(text)
        return

    p = doc.add_paragraph(style=style) if style else doc.add_paragraph()
    for part in parts:
        if not part:
            continue
        if _INLINE_MATH_RE.fullmatch(part):
            if not insert_math_into_paragraph(p, part):
                run = p.add_run(part)
                run.italic = True
                run.font.name = "Cambria Math"
        else:
            p.add_run(part)


def _apply_korean_font_to_style(style, font_name: str) -> None:
    """Set ascii/hAnsi/eastAsia/cs fonts on a style so Korean text renders properly."""
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
        rfonts.set(qn(f"w:{attr}"), font_name)


def _configure_korean_fonts(doc: Document, font_name: str = KOREAN_FONT) -> None:
    """Apply the Korean font to Normal + Heading styles so the whole document inherits it."""
    style_names = ["Normal", "Title"] + [f"Heading {i}" for i in range(1, 10)] + [
        "List Bullet",
        "List Number",
        "Caption",
    ]
    for name in style_names:
        try:
            style = doc.styles[name]
        except KeyError:
            continue
        try:
            _apply_korean_font_to_style(style, font_name)
        except Exception:
            log.warning("could not set Korean font on style %s", name)


def build_docx(
    translated: list[TranslatedElement],
    output_path: str | Path,
    *,
    title: str | None = None,
) -> Path:
    doc = Document()
    _configure_korean_fonts(doc)
    if title:
        doc.add_heading(title, level=0)

    last_page = None
    for tr in translated:
        elem = tr.element
        if last_page is not None and elem.page != last_page:
            doc.add_paragraph()
        last_page = elem.page

        cat = elem.category.lower()
        if cat in IMAGE_CATEGORIES:
            placed = bool(elem.base64) and _add_image_from_base64(doc, elem.base64 or "")
            if not placed and elem.markdown.strip():
                # No image returned by Document Parse — fall back to chart_recognition markdown
                # so axis labels, legends, and table data aren't lost.
                p = doc.add_paragraph()
                p.add_run("[차트 데이터]\n").bold = True
                p.add_run(elem.markdown.strip())
            if tr.translated_text.strip():
                cap = doc.add_paragraph(tr.translated_text.strip())
                cap.italic = True
            continue

        if cat in TABLE_CATEGORIES:
            _add_html_table(doc, tr.translated_html or elem.html)
            continue

        if cat == "equation":
            _add_equation_paragraph(doc, elem.text or elem.markdown)
            continue

        if cat in HEADING_LEVELS:
            doc.add_heading(tr.translated_text or elem.text, level=HEADING_LEVELS[cat])
            continue

        if cat == "list":
            for line in (tr.translated_text or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                _add_text_with_inline_math(doc, line.lstrip("-•* "), style="List Bullet")
            continue

        text = tr.translated_text.strip()
        if not text:
            continue
        _add_text_with_inline_math(doc, text)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)
    return output_path


def _try_docx2pdf(docx_path: Path, pdf_path: Path) -> bool:
    try:
        from docx2pdf import convert
    except ImportError:
        return False
    try:
        convert(str(docx_path), str(pdf_path))
    except Exception:
        log.exception("docx2pdf conversion failed")
        return False
    return pdf_path.exists()


def _find_soffice() -> str | None:
    import shutil

    for name in ("soffice", "libreoffice", "soffice.exe"):
        path = shutil.which(name)
        if path:
            return path
    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/soffice",
        "/usr/bin/libreoffice",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _try_libreoffice(docx_path: Path, pdf_path: Path) -> bool:
    import subprocess

    soffice = _find_soffice()
    if not soffice:
        return False
    out_dir = pdf_path.parent
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)],
            check=True,
            timeout=180,
            capture_output=True,
        )
    except Exception:
        log.exception("LibreOffice conversion failed")
        return False
    produced = out_dir / (docx_path.stem + ".pdf")
    if produced.exists() and produced != pdf_path:
        produced.replace(pdf_path)
    return pdf_path.exists()


def docx_to_pdf(docx_path: str | Path, pdf_path: str | Path | None = None) -> Path | None:
    """Convert DOCX to PDF, trying docx2pdf first then LibreOffice headless.

    docx2pdf works on Windows/macOS with MS Word installed.
    LibreOffice headless works on Linux/Docker and any host that has it.
    Returns the produced PDF path, or None if no backend succeeded.
    """
    docx_path = Path(docx_path)
    pdf_path = Path(pdf_path) if pdf_path else docx_path.with_suffix(".pdf")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    if _try_docx2pdf(docx_path, pdf_path):
        log.info("PDF produced via docx2pdf: %s", pdf_path)
        return pdf_path
    if _try_libreoffice(docx_path, pdf_path):
        log.info("PDF produced via LibreOffice: %s", pdf_path)
        return pdf_path
    log.warning("No PDF backend available; install MS Word or LibreOffice")
    return None
