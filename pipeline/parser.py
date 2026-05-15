"""Upstage Document Parse wrapper.

POST https://api.upstage.ai/v1/document-ai/document-parse
multipart/form-data: document=<file>
form fields: output_formats, coordinates, base64_encoding, chart_recognition, model, ocr

Response (relevant fields):
{
  "elements": [
    {
      "id": 0,
      "page": 1,
      "category": "paragraph" | "heading1" | "table" | "figure" | "equation" | ...,
      "content": {"text": "...", "html": "...", "markdown": "..."},
      "coordinates": [{"x": 0.1, "y": 0.2}, ...],
      "base64_encoding": "..."  // only for table/figure when requested
    },
    ...
  ],
  "content": {"text": "...", "html": "...", "markdown": "..."}
}
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

DOCUMENT_PARSE_URL = "/document-ai/document-parse"

# Upstage Document Parse rejects PDFs over 100 pages with HTTP 413. We
# split larger PDFs into chunks and stitch the parsed results back. 90 is
# a safety margin below the hard limit.
PARSE_PAGE_LIMIT = 90


@dataclass
class Element:
    id: int
    page: int
    category: str
    text: str = ""
    html: str = ""
    markdown: str = ""
    base64: str | None = None
    coordinates: list[dict[str, float]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    elements: list[Element]
    full_text: str
    full_html: str
    full_markdown: str
    raw: dict[str, Any]


class DocumentParser:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.upstage.ai/v1",
        model: str = "document-parse",
        timeout: float = 600.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def parse(self, file_path: str | Path) -> ParsedDocument:
        """Parse a PDF/document. PDFs over PARSE_PAGE_LIMIT pages are split
        into chunks, each parsed separately, and the resulting elements
        merged with corrected page numbers and contiguous ids."""
        file_path = Path(file_path)
        if file_path.suffix.lower() == ".pdf":
            pages = _pdf_page_count(file_path)
            if pages is not None and pages > PARSE_PAGE_LIMIT:
                log.info(
                    "Document Parse: %s has %d pages (> %d limit); splitting",
                    file_path.name, pages, PARSE_PAGE_LIMIT,
                )
                return self._parse_chunked(file_path, total_pages=pages)
        return self._parse_single(file_path)

    def _parse_single(self, file_path: Path) -> ParsedDocument:
        url = f"{self.base_url}{DOCUMENT_PARSE_URL}"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        data = {
            "model": self.model,
            "ocr": "auto",
            "coordinates": "true",
            "chart_recognition": "true",
            "output_formats": json.dumps(["text", "html", "markdown"]),
            "base64_encoding": json.dumps(["figure", "chart", "table"]),
        }

        with file_path.open("rb") as f:
            files = {"document": (file_path.name, f, "application/pdf")}
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, headers=headers, data=data, files=files)

        if resp.status_code >= 400:
            raise RuntimeError(
                f"Document Parse failed {resp.status_code}: {resp.text[:500]}"
            )

        payload = resp.json()
        return self._build(payload)

    def _parse_chunked(self, file_path: Path, *, total_pages: int) -> ParsedDocument:
        """Split a large PDF into PARSE_PAGE_LIMIT-page chunks, parse each
        in parallel, then merge elements with page-number offsets and
        contiguous ids. Falls back to single parse if splitting fails."""
        try:
            import fitz  # PyMuPDF
        except ImportError as e:
            raise RuntimeError(
                "PDF has > 100 pages and pymupdf is not installed — "
                "cannot split. Install pymupdf or upload a smaller PDF."
            ) from e

        # Build chunk PDFs in a temp directory next to the source.
        chunk_dir = file_path.parent / f"{file_path.stem}__chunks"
        chunk_dir.mkdir(exist_ok=True)
        chunk_paths: list[tuple[int, Path]] = []  # (page_offset, chunk_pdf_path)
        try:
            with fitz.open(str(file_path)) as src:
                for start in range(0, total_pages, PARSE_PAGE_LIMIT):
                    end = min(start + PARSE_PAGE_LIMIT, total_pages)
                    chunk_doc = fitz.open()
                    chunk_doc.insert_pdf(src, from_page=start, to_page=end - 1)
                    chunk_path = chunk_dir / f"pages_{start+1:04d}_{end:04d}.pdf"
                    chunk_doc.save(str(chunk_path))
                    chunk_doc.close()
                    chunk_paths.append((start, chunk_path))
            log.info("Document Parse: split into %d chunks", len(chunk_paths))

            # Parse chunks in parallel. Workers kept small — Upstage may
            # rate-limit if we fan out too aggressively.
            workers = min(3, len(chunk_paths))
            results: list[ParsedDocument | None] = [None] * len(chunk_paths)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {
                    ex.submit(self._parse_single, p): idx
                    for idx, (_, p) in enumerate(chunk_paths)
                }
                for fut in as_completed(futures):
                    idx = futures[fut]
                    results[idx] = fut.result()  # let exceptions propagate
                    log.info(
                        "Document Parse: chunk %d/%d done (%d elements)",
                        idx + 1, len(chunk_paths),
                        len(results[idx].elements) if results[idx] else 0,
                    )

            # Merge in chunk order so page numbers stay monotonic.
            merged_elements: list[Element] = []
            merged_texts: list[str] = []
            merged_htmls: list[str] = []
            merged_mds: list[str] = []
            next_id = 0
            for (page_offset, _), parsed in zip(chunk_paths, results):
                if parsed is None:
                    continue
                for elem in parsed.elements:
                    elem.id = next_id
                    elem.page = elem.page + page_offset
                    merged_elements.append(elem)
                    next_id += 1
                if parsed.full_text:
                    merged_texts.append(parsed.full_text)
                if parsed.full_html:
                    merged_htmls.append(parsed.full_html)
                if parsed.full_markdown:
                    merged_mds.append(parsed.full_markdown)

            log.info(
                "Document Parse: merged %d elements from %d chunks (pages 1..%d)",
                len(merged_elements), len(chunk_paths), total_pages,
            )
            return ParsedDocument(
                elements=merged_elements,
                full_text="\n\n".join(merged_texts),
                full_html="\n\n".join(merged_htmls),
                full_markdown="\n\n".join(merged_mds),
                raw={
                    "chunked": True,
                    "total_pages": total_pages,
                    "chunk_count": len(chunk_paths),
                },
            )
        finally:
            # Clean up chunk files + dir (best-effort).
            for _, p in chunk_paths:
                try:
                    p.unlink()
                except Exception:
                    pass
            try:
                chunk_dir.rmdir()
            except Exception:
                pass

    @staticmethod
    def _extract_base64(raw: dict[str, Any]) -> str | None:
        """Try several known shapes for the base64 image payload in Document Parse responses."""
        for key in ("base64_encoding", "base64", "base64Image", "image_base64", "image"):
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, dict):
                for inner in ("base64", "data", "value"):
                    iv = v.get(inner)
                    if isinstance(iv, str) and iv.strip():
                        return iv
        content = raw.get("content") or {}
        if isinstance(content, dict):
            for key in ("base64", "base64_encoding", "image"):
                v = content.get(key)
                if isinstance(v, str) and v.strip():
                    return v
        return None

    @staticmethod
    def _build(payload: dict[str, Any]) -> ParsedDocument:
        elements: list[Element] = []
        for raw in payload.get("elements", []):
            content = raw.get("content") or {}
            elements.append(
                Element(
                    id=raw.get("id", 0),
                    page=raw.get("page", 1),
                    category=raw.get("category", "paragraph"),
                    text=content.get("text", "") or "",
                    html=content.get("html", "") or "",
                    markdown=content.get("markdown", "") or "",
                    base64=DocumentParser._extract_base64(raw),
                    coordinates=raw.get("coordinates", []) or [],
                    raw=raw,
                )
            )

        full = payload.get("content") or {}
        return ParsedDocument(
            elements=elements,
            full_text=full.get("text", "") or "",
            full_html=full.get("html", "") or "",
            full_markdown=full.get("markdown", "") or "",
            raw=payload,
        )


def _pdf_page_count(path: Path) -> int | None:
    """Quick page-count probe. Returns None if pymupdf is unavailable or
    the file can't be opened — callers should fall back to single-shot
    parsing in that case."""
    try:
        import fitz
    except ImportError:
        return None
    try:
        with fitz.open(str(path)) as doc:
            return len(doc)
    except Exception:
        log.exception("could not read page count for %s", path)
        return None


def from_env() -> DocumentParser:
    api_key = os.environ.get("UPSTAGE_API_KEY")
    if not api_key:
        raise RuntimeError("UPSTAGE_API_KEY not set")
    base_url = os.environ.get("UPSTAGE_BASE_URL", "https://api.upstage.ai/v1")
    model = os.environ.get("DOCUMENT_PARSE_MODEL", "document-parse")
    return DocumentParser(api_key=api_key, base_url=base_url, model=model)
