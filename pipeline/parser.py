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
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

DOCUMENT_PARSE_URL = "/document-ai/document-parse"


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
        url = f"{self.base_url}{DOCUMENT_PARSE_URL}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        file_path = Path(file_path)

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


def from_env() -> DocumentParser:
    api_key = os.environ.get("UPSTAGE_API_KEY")
    if not api_key:
        raise RuntimeError("UPSTAGE_API_KEY not set")
    base_url = os.environ.get("UPSTAGE_BASE_URL", "https://api.upstage.ai/v1")
    model = os.environ.get("DOCUMENT_PARSE_MODEL", "document-parse")
    return DocumentParser(api_key=api_key, base_url=base_url, model=model)
