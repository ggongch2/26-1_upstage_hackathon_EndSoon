"""LaTeX → MathML → OMML, then inject into a python-docx paragraph.

OMML(Office MathML)은 Word가 네이티브로 렌더링하는 수식 XML. 표준 변환 경로는
LaTeX → MathML → MS가 배포한 MML2OMML.XSL → OMML.

XSL 파일이 없으면 polynomial fallback으로 LaTeX 원문을 monospace 텍스트로 삽입한다.
XSL은 데모용 mini 버전을 함께 제공한다 (대부분의 기본 연산자/분수/지수/행렬 커버).
"""
from __future__ import annotations

import logging
from pathlib import Path

from docx.oxml.ns import qn
from lxml import etree

log = logging.getLogger(__name__)

OMML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_XSL_PATH = Path(__file__).parent / "assets" / "MML2OMML.XSL"
_xsl_cache: etree.XSLT | None = None


def _load_xsl() -> etree.XSLT | None:
    global _xsl_cache
    if _xsl_cache is not None:
        return _xsl_cache
    if not _XSL_PATH.exists():
        log.warning("MML2OMML.XSL not found at %s; math will fall back to LaTeX text", _XSL_PATH)
        return None
    try:
        tree = etree.parse(str(_XSL_PATH))
        _xsl_cache = etree.XSLT(tree)
        return _xsl_cache
    except Exception:
        log.exception("Failed to load MML2OMML.XSL")
        return None


def latex_to_omml(latex: str) -> etree._Element | None:
    """Convert LaTeX (with or without $...$ delimiters) to an OMML <m:oMath> element."""
    expr = latex.strip()
    for delim in ("$$", "$", r"\(", r"\)", r"\[", r"\]"):
        expr = expr.replace(delim, "")
    expr = expr.strip()
    if not expr:
        return None

    try:
        import latex2mathml.converter

        mathml = latex2mathml.converter.convert(expr)
    except Exception:
        log.exception("LaTeX→MathML conversion failed for %r", expr[:80])
        return None

    xsl = _load_xsl()
    if xsl is None:
        return None
    try:
        mathml_tree = etree.fromstring(mathml.encode("utf-8"))
        omml_tree = xsl(mathml_tree)
    except Exception:
        log.exception("MathML→OMML XSLT failed for %r", expr[:80])
        return None

    root = omml_tree.getroot() if hasattr(omml_tree, "getroot") else omml_tree
    if root is None:
        return None
    return root


def insert_math_into_paragraph(paragraph, latex: str) -> bool:
    """Append a real OMML equation to the given python-docx Paragraph. Returns True on success."""
    omml = latex_to_omml(latex)
    if omml is None:
        return False
    try:
        paragraph._p.append(omml)
        return True
    except Exception:
        log.exception("Failed to append OMML element to paragraph")
        return False
