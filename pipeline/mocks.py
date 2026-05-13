"""Offline mock implementations to validate the pipeline without Upstage API.

Activated by MOCK_MODE=true env var. Returns a deterministic fake document so
the docx builder, web UI, and PDF pipeline can be exercised end-to-end before
real API keys are available.
"""
from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from .glossary import Glossary
from .parser import Element, ParsedDocument


def _fake_png() -> str:
    img = Image.new("RGB", (480, 240), color=(240, 244, 252))
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 470, 230], outline=(60, 90, 160), width=3)
    draw.text((30, 100), "[Figure 1] Sample chart placeholder", fill=(40, 60, 120))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


SAMPLE_FULL_TEXT = (
    "Chapter 1 Introduction to Linear Algebra. "
    "A matrix is a rectangular array of numbers, symbols, or expressions, "
    "arranged in rows and columns. Eigenvalues and eigenvectors characterize "
    "linear transformations. The determinant of a square matrix encodes "
    "the volume scaling factor of the linear map it represents."
)


def fake_parsed_document() -> ParsedDocument:
    elems = [
        Element(id=0, page=1, category="heading1", text="Chapter 1. Linear Algebra Basics"),
        Element(
            id=1,
            page=1,
            category="paragraph",
            text=(
                "A matrix is a rectangular array of numbers arranged in rows and columns. "
                "Matrices are fundamental objects in linear algebra and appear throughout "
                "engineering, physics, and computer science."
            ),
        ),
        Element(
            id=2,
            page=1,
            category="equation",
            text=r"$A = \begin{bmatrix} a_{11} & a_{12} \\ a_{21} & a_{22} \end{bmatrix}$",
        ),
        Element(
            id=3,
            page=1,
            category="paragraph",
            text=(
                "The determinant of a 2x2 matrix encodes the signed area of the "
                "parallelogram spanned by its column vectors."
            ),
        ),
        Element(
            id=4,
            page=1,
            category="equation",
            text=r"$\det(A) = a_{11} a_{22} - a_{12} a_{21}$",
        ),
        Element(
            id=5,
            page=2,
            category="heading2",
            text="1.1 Eigenvalues and Eigenvectors",
        ),
        Element(
            id=6,
            page=2,
            category="paragraph",
            text=(
                "An eigenvector of a square matrix A is a nonzero vector v such that "
                "A v is a scalar multiple of v. The scalar is called the eigenvalue."
            ),
        ),
        Element(id=7, page=2, category="equation", text=r"$A v = \lambda v$"),
        Element(
            id=8,
            page=2,
            category="figure",
            text="Figure 1. Eigenvector geometric intuition.",
            base64=_fake_png(),
        ),
        Element(
            id=9,
            page=2,
            category="table",
            html=(
                "<table><tr><th>Matrix</th><th>Eigenvalues</th></tr>"
                "<tr><td>Identity</td><td>1, 1</td></tr>"
                "<tr><td>Rotation 90°</td><td>i, -i</td></tr></table>"
            ),
        ),
        Element(
            id=10,
            page=2,
            category="paragraph",
            text=(
                "Eigendecomposition expresses a diagonalizable matrix as a product of "
                "its eigenvectors and a diagonal matrix of its eigenvalues."
            ),
        ),
    ]
    return ParsedDocument(
        elements=elems,
        full_text=SAMPLE_FULL_TEXT,
        full_html="",
        full_markdown="",
        raw={"mock": True},
    )


class MockParser:
    def parse(self, file_path) -> ParsedDocument:  # noqa: ANN001
        return fake_parsed_document()


class MockSolar:
    """Returns deterministic Korean translations for known sample sentences."""

    _TRANSLATIONS: dict[str, str] = {
        "Chapter 1. Linear Algebra Basics": "1장. 선형대수학 기초",
        "1.1 Eigenvalues and Eigenvectors": "1.1 고윳값과 고유벡터",
        "Figure 1. Eigenvector geometric intuition.": "그림 1. 고유벡터의 기하학적 직관.",
    }
    _DEFAULT = (
        "[모의 번역] 본 문장은 Mock 모드에서 생성된 한국어 번역 자리표시자입니다. "
        "실제 API 연결 시 Solar LLM이 자연스러운 학술 한국어로 변환합니다."
    )

    def chat(self, messages, **kwargs):  # noqa: ANN001
        last = messages[-1]["content"] if messages else ""
        for en, ko in self._TRANSLATIONS.items():
            if en in last:
                return ko
        if "JSON" in last or "json" in last:
            return (
                '{"matrix": "행렬", "eigenvalue": "고윳값", "eigenvector": "고유벡터",'
                ' "determinant": "행렬식", "linear transformation": "선형변환"}'
            )
        return self._DEFAULT


def fake_glossary() -> Glossary:
    return Glossary(
        mapping={
            "matrix": "행렬",
            "eigenvalue": "고윳값",
            "eigenvector": "고유벡터",
            "determinant": "행렬식",
            "linear transformation": "선형변환",
            "eigendecomposition": "고유분해",
        }
    )
