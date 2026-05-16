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
from pydantic import BaseModel

from pipeline import docx_builder, glossary as glossary_mod, mocks, parser as parser_mod, solar as solar_mod
from pipeline.jobs import REGISTRY
from pipeline.pdf_crop import PdfCropper
from pipeline.translator import translate_elements


class GlossaryUpdate(BaseModel):
    glossary: dict[str, str]

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

# Cap concurrent pipeline runs so we don't fan out beyond what the API
# pool can handle. Jobs over the cap sit in the 'queued' stage until a
# slot frees. Tune with MAX_CONCURRENT_JOBS env var.
try:
    _MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("MAX_CONCURRENT_JOBS", "2")))
except ValueError:
    _MAX_CONCURRENT_JOBS = 2
_JOB_SEMAPHORE = threading.Semaphore(_MAX_CONCURRENT_JOBS)

_key_count = len(solar_mod.parse_keys(os.environ.get("UPSTAGE_API_KEY", "")))
log.info(
    "config: max_concurrent_jobs=%d, upstage_api_keys=%d",
    _MAX_CONCURRENT_JOBS, _key_count,
)

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


def _run_pipeline(
    job_id: str,
    work_path: Path,
    want_pdf: bool,
    title: str | None,
    interactive: bool,
) -> None:
    # Throttle concurrent pipelines. Try non-blocking first so we can
    # surface a "queued" status when all slots are busy.
    acquired = _JOB_SEMAPHORE.acquire(blocking=False)
    if not acquired:
        REGISTRY.set_stage(
            job_id, "queued",
            message=f"처리 슬롯 대기 중 (최대 동시 {_MAX_CONCURRENT_JOBS}개)",
        )
        log.info("job %s :: queued — waiting for free slot", job_id)
        _JOB_SEMAPHORE.acquire()
        log.info("job %s :: slot acquired", job_id)

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

        # Element-level readable dumps for manual verification.
        # *_parsed.jsonl — one element per line, scriptable with jq/grep.
        # *_parsed.md   — eyes-friendly: page-grouped, base64 elided.
        try:
            import json

            jsonl_path = OUTPUT_DIR / f"{job_id}_parsed.jsonl"
            md_path = OUTPUT_DIR / f"{job_id}_parsed.md"

            with jsonl_path.open("w", encoding="utf-8") as f:
                for e in parsed.elements:
                    f.write(json.dumps({
                        "id": e.id,
                        "page": e.page,
                        "category": e.category,
                        "text": e.text,
                        "html": e.html if e.category.lower() == "table" else "",
                        "markdown": e.markdown,
                        "has_base64": bool(e.base64),
                        "base64_bytes": len(e.base64) if e.base64 else 0,
                        "coordinates": e.coordinates,
                    }, ensure_ascii=False) + "\n")

            md_lines: list[str] = [
                f"# Parsed dump — job {job_id}",
                "",
                f"- Source: `{work_path.name}`",
                f"- Elements: {len(parsed.elements)}",
                "",
            ]
            current_page = None
            for e in parsed.elements:
                if e.page != current_page:
                    current_page = e.page
                    md_lines.append(f"\n## Page {current_page}\n")
                tag = f"`[{e.id:>4} · {e.category}]`"
                if e.base64:
                    tag += f" *(base64 {len(e.base64)} chars attached)*"
                md_lines.append(tag)
                body = (e.text or "").strip()
                if body:
                    md_lines.append("")
                    md_lines.append("> " + body.replace("\n", "\n> "))
                elif e.category.lower() == "table" and e.html:
                    md_lines.append("")
                    md_lines.append("```html")
                    md_lines.append(e.html.strip()[:1200])
                    if len(e.html) > 1200:
                        md_lines.append(f"... (+{len(e.html)-1200} chars)")
                    md_lines.append("```")
                md_lines.append("")
            md_path.write_text("\n".join(md_lines), encoding="utf-8")
            log.info(
                "job %s :: parsed dumps → %s , %s",
                job_id, jsonl_path.name, md_path.name,
            )
        except Exception:
            log.exception("parsed dump failed (non-fatal)")

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

        # Expose extracted mapping so it's available in /jobs/{id}/progress
        # even before the user opens the review UI.
        REGISTRY.update(job_id, glossary=dict(glossary.mapping))

        if interactive:
            log.info("job %s :: awaiting glossary review", job_id)
            edited = REGISTRY.await_glossary_review(job_id, glossary.mapping)
            # If the job was failed/cancelled mid-wait, await returns the
            # original mapping and we'd just keep going; the next state mutation
            # would no-op anyway because the job is in 'error' state.
            if edited != glossary.mapping:
                log.info(
                    "job %s :: glossary edited by user (%d → %d terms)",
                    job_id, len(glossary.mapping), len(edited),
                )
            glossary = glossary_mod.Glossary(mapping=edited)

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
        docx_builder.build_docx(
            translated,
            out_docx,
            title=title or work_path.stem,
            source_pdf=work_path if work_path.suffix.lower() == ".pdf" and not mock_mode else None,
        )
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
    finally:
        _JOB_SEMAPHORE.release()


@app.post("/translate")
async def translate(
    file: UploadFile = File(...),
    want_pdf: bool = Form(False),
    title: str | None = Form(None),
    interactive: bool = Form(False),
) -> JSONResponse:
    if not file.filename:
        raise HTTPException(400, "missing filename")

    job_id = uuid.uuid4().hex[:12]
    work_path = WORK_DIR / f"{job_id}_{file.filename}"
    work_path.write_bytes(await file.read())
    log.info("job %s :: saved upload to %s interactive=%s", job_id, work_path, interactive)

    REGISTRY.create(job_id)
    REGISTRY.set_stage(job_id, "upload", message=f"{file.filename} 수신 완료")

    thread = threading.Thread(
        target=_run_pipeline,
        args=(job_id, work_path, want_pdf, title, interactive),
        daemon=True,
        name=f"pipeline-{job_id}",
    )
    thread.start()

    return JSONResponse(
        {"job_id": job_id, "status": "queued", "interactive": interactive},
        status_code=202,
    )


@app.get("/jobs/{job_id}/progress")
def job_progress(job_id: str) -> JSONResponse:
    state = REGISTRY.get(job_id)
    if state is None:
        raise HTTPException(404, "job not found")
    return JSONResponse(state.to_dict())


@app.post("/jobs/{job_id}/glossary")
def update_glossary(job_id: str, body: GlossaryUpdate) -> JSONResponse:
    """Receive the user-edited glossary and resume the paused pipeline thread."""
    state = REGISTRY.get(job_id)
    if state is None:
        raise HTTPException(404, "job not found")
    if state.stage != "awaiting_review":
        raise HTTPException(
            409, f"job is not awaiting review (current stage: {state.stage})"
        )
    cleaned = {
        str(k).strip(): str(v).strip()
        for k, v in body.glossary.items()
        if str(k).strip() and str(v).strip()
    }
    ok = REGISTRY.resume_glossary_review(job_id, cleaned)
    if not ok:
        raise HTTPException(409, "no waiter to resume (race condition)")
    log.info("job %s :: glossary review resumed with %d terms", job_id, len(cleaned))
    return JSONResponse({"status": "resumed", "term_count": len(cleaned)})


@app.get("/download/{name}")
def download(name: str) -> FileResponse:
    target = OUTPUT_DIR / name
    if not target.exists() or ".." in name:
        raise HTTPException(404, "not found")
    return FileResponse(target, filename=name)
