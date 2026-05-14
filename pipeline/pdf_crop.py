"""Crop arbitrary regions out of a source PDF, given Document Parse coordinates.

Document Parse returns element bboxes as 4 corner points with x/y normalized
to [0,1] of the page. We map those to absolute PDF coordinates, render the
region at a configurable DPI, and return base64 PNG bytes ready to embed in
the output document.

Used to preserve table/equation visual fidelity when OCR-to-text conversion
mangles them (broken matrix layouts, mis-segmented numerators, etc.).
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

# Default render DPI. Higher = sharper image but larger DOCX. 200 is the sweet
# spot for readable math at typical Word page sizes.
DEFAULT_DPI = 200
PADDING_POINTS = 4.0


class PdfCropper:
    """Wraps a PyMuPDF document handle for repeated crops without reopening."""

    def __init__(self, pdf_path: str | Path) -> None:
        try:
            import fitz  # PyMuPDF
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pymupdf not installed — `pip install pymupdf`") from e
        self._fitz = fitz
        self._path = Path(pdf_path)
        self._doc = fitz.open(str(self._path))

    def close(self) -> None:
        try:
            self._doc.close()
        except Exception:
            pass

    def __enter__(self) -> "PdfCropper":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def crop_to_png_bytes(
        self,
        page: int,
        coords: Sequence[dict],
        *,
        dpi: int = DEFAULT_DPI,
    ) -> bytes | None:
        if not coords or len(coords) < 2:
            return None
        if page < 1 or page > len(self._doc):
            log.warning("crop: page %d out of range (1..%d)", page, len(self._doc))
            return None
        pdf_page = self._doc[page - 1]
        page_w = pdf_page.rect.width
        page_h = pdf_page.rect.height

        xs = [c.get("x", 0) * page_w for c in coords]
        ys = [c.get("y", 0) * page_h for c in coords]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        x0 = max(0.0, x0 - PADDING_POINTS)
        y0 = max(0.0, y0 - PADDING_POINTS)
        x1 = min(page_w, x1 + PADDING_POINTS)
        y1 = min(page_h, y1 + PADDING_POINTS)
        if x1 <= x0 or y1 <= y0:
            log.warning("crop: degenerate bbox p=%d coords=%s", page, coords)
            return None

        rect = self._fitz.Rect(x0, y0, x1, y1)
        zoom = dpi / 72.0
        try:
            pix = pdf_page.get_pixmap(
                matrix=self._fitz.Matrix(zoom, zoom),
                clip=rect,
                alpha=False,
            )
            return pix.tobytes("png")
        except Exception:
            log.exception("crop: pixmap failed p=%d", page)
            return None

    def crop_to_base64(
        self,
        page: int,
        coords: Sequence[dict],
        *,
        dpi: int = DEFAULT_DPI,
    ) -> str | None:
        png = self.crop_to_png_bytes(page, coords, dpi=dpi)
        if png is None:
            return None
        return base64.b64encode(png).decode("ascii")
