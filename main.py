"""FastAPI entrypoint: PDF in → async translation pipeline with real-time progress."""
from __future__ import annotations

import logging
import os
import threading
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pipeline import docx_builder, glossary as glossary_mod, mocks, parser as parser_mod, solar as solar_mod
from pipeline.jobs import REGISTRY
from pipeline.pdf_crop import PdfCropper
from pipeline.translator import translate_elements

# Element categories where we capture an image crop from the original PDF
# instead of trusting Document Parse's text/HTML extraction.
CAPTURE_AS_IMAGE = {"table", "equation"}


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "on"}


load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("main")

WORK_DIR = Path(os.environ.get("WORK_DIR", "./work"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./output"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="STEM Textbook Translator", version="0.1.0")

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/mode")
def mode() -> dict[str, bool]:
    return {"mock": _truthy(os.environ.get("MOCK_MODE"))}


def _run_pipeline(job_id: str, work_path: Path, want_pdf: bool, title: str | None) -> None:
    mock_mode = _truthy(os.environ.get("MOCK_MODE"))
    try:
        try:
            parser = mocks.MockParser() if mock_mode else parser_mod.from_env()
            solar = mocks.MockSolar() if mock_mode else solar_mod.from_env()
        except RuntimeError as e:
            REGISTRY.fail(job_id, str(e))
            return

        REGISTRY.set_stage(job_id, "parse", message="Document Parse 호출 중…")
        log.info("job %s :: Document Parse start (mock=%s)", job_id, mock_mode)
        parsed = parser.parse(work_path)
        log.info("job %s :: %d elements parsed", job_id, len(parsed.elements))

        # Crop original PDF regions for table/equation elements so the docx
        # gets pixel-perfect images instead of OCR-mangled text. Skipped in
        # mock mode (no real PDF to crop from) and on PDFs that pymupdf can't open.
        if not mock_mode and work_path.suffix.lower() == ".pdf":
            try:
                cropped = 0
                with PdfCropper(work_path) as cropper:
                    for elem in parsed.elements:
                        if elem.category.lower() not in CAPTURE_AS_IMAGE:
                            continue
                        if elem.base64:
                            continue
                        if not elem.coordinates:
                            continue
                        b64 = cropper.crop_to_base64(elem.page, elem.coordinates)
                        if b64:
                            elem.base64 = b64
                            cropped += 1
                log.info("job %s :: cropped %d table/equation regions from PDF", job_id, cropped)
            except Exception:
                log.exception("job %s :: PDF cropping failed (non-fatal); falling back to text", job_id)

        # Dump raw response for debugging — category counts + first raw element of each
        # category with long string fields truncated. This is the file to share when
        # debugging "why isn't <thing> coming through".
        try:
            import json
            from collections import Counter

            BASE64_KEYS = {"base64_encoding", "base64", "base64Image", "image_base64"}

            def _truncate(v, *, key: str | None = None):  # type: ignore[no-untyped-def]
                if isinstance(v, str):
                    if key in BASE64_KEYS:
                        return v  # keep full so image debugging can replay locally
                    return v if len(v) <= 400 else v[:400] + f"…(+{len(v)-400} chars)"
                if isinstance(v, dict):
                    return {k: _truncate(x, key=k) for k, x in v.items()}
                if isinstance(v, list):
                    return [_truncate(x) for x in v[:10]] + ([f"…(+{len(v)-10})"] if len(v) > 10 else [])
                return v

            cats = Counter(e.category for e in parsed.elements)
            raw_samples: dict[str, list] = {}
            for e in parsed.elements:
                bucket = raw_samples.setdefault(e.category, [])
                if len(bucket) < 2:
                    bucket.append(_truncate(e.raw))

            debug_path = OUTPUT_DIR / f"{job_id}_raw.json"
            debug_path.write_text(
                json.dumps(
                    {
                        "category_counts": dict(cats),
                        "raw_samples_per_category": raw_samples,
                    },
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            log.info("job %s :: raw debug dump → %s", job_id, debug_path)
        except Exception:
            log.exception("raw dump failed (non-fatal)")

        REGISTRY.set_stage(
            job_id, "glossary", total=0, message=f"{len(parsed.elements)}개 요소에서 용어집 추출"
        )
        if mock_mode:
            glossary = mocks.fake_glossary()
        else:
            def _gloss_cb(done: int, total: int) -> None:
                REGISTRY.update(job_id, processed=done, total=total)

            glossary = glossary_mod.build_glossary(solar, parsed.full_text, on_progress=_gloss_cb)
        log.info("job %s :: glossary terms=%d", job_id, len(glossary.mapping))

        REGISTRY.set_stage(
            job_id,
            "translate",
            total=len(parsed.elements),
            message=f"{len(glossary.mapping)}개 용어로 일관 번역",
        )

        def _trans_cb(done: int, total: int) -> None:
            REGISTRY.update(job_id, processed=done, total=total)

        translated = translate_elements(solar, glossary, parsed.elements, on_progress=_trans_cb)

        REGISTRY.set_stage(job_id, "docx", message="Word 문서 조립 중")
        out_docx = OUTPUT_DIR / f"{job_id}_translated.docx"
        docx_builder.build_docx(translated, out_docx, title=title or work_path.stem)
        log.info("job %s :: wrote %s", job_id, out_docx)

        out_pdf = None
        if want_pdf:
            REGISTRY.set_stage(job_id, "pdf", message="PDF 변환 중")
            out_pdf = docx_builder.docx_to_pdf(out_docx)
            log.info("job %s :: pdf=%s", job_id, out_pdf)

        result = {
            "job_id": job_id,
            "docx": str(out_docx),
            "pdf": str(out_pdf) if out_pdf else None,
            "glossary": glossary.mapping,
            "element_count": len(parsed.elements),
            "download_docx": f"/download/{out_docx.name}",
            "download_pdf": f"/download/{out_pdf.name}" if out_pdf else None,
        }
        REGISTRY.finish(job_id, result)
        log.info("job %s :: done", job_id)
    except Exception as e:
        log.exception("job %s :: pipeline failed", job_id)
        REGISTRY.fail(job_id, f"{type(e).__name__}: {e}")


@app.post("/translate")
async def translate(
    file: UploadFile = File(...),
    want_pdf: bool = Form(False),
    title: str | None = Form(None),
) -> JSONResponse:
    if not file.filename:
        raise HTTPException(400, "missing filename")

    job_id = uuid.uuid4().hex[:12]
    work_path = WORK_DIR / f"{job_id}_{file.filename}"
    work_path.write_bytes(await file.read())
    log.info("job %s :: saved upload to %s", job_id, work_path)

    REGISTRY.create(job_id)
    REGISTRY.set_stage(job_id, "upload", message=f"{file.filename} 수신 완료")

    thread = threading.Thread(
        target=_run_pipeline,
        args=(job_id, work_path, want_pdf, title),
        daemon=True,
        name=f"pipeline-{job_id}",
    )
    thread.start()

    return JSONResponse({"job_id": job_id, "status": "queued"}, status_code=202)


@app.get("/jobs/{job_id}/progress")
def job_progress(job_id: str) -> JSONResponse:
    state = REGISTRY.get(job_id)
    if state is None:
        raise HTTPException(404, "job not found")
    return JSONResponse(state.to_dict())


@app.get("/download/{name}")
def download(name: str) -> FileResponse:
    target = OUTPUT_DIR / name
    if not target.exists() or ".." in name:
        raise HTTPException(404, "not found")
    return FileResponse(target, filename=name)
