"""FastAPI entrypoint: PDF in → translated DOCX (and optional PDF) out."""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pipeline import docx_builder, glossary as glossary_mod, mocks, parser as parser_mod, solar as solar_mod
from pipeline.translator import translate_elements


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "on"}

load_dotenv()
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

    mock_mode = _truthy(os.environ.get("MOCK_MODE"))
    try:
        parser = mocks.MockParser() if mock_mode else parser_mod.from_env()
        solar = mocks.MockSolar() if mock_mode else solar_mod.from_env()
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    log.info("job %s :: Document Parse start (mock=%s)", job_id, mock_mode)
    parsed = parser.parse(work_path)
    log.info("job %s :: %d elements parsed", job_id, len(parsed.elements))

    log.info("job %s :: building glossary", job_id)
    if mock_mode:
        glossary = mocks.fake_glossary()
    else:
        glossary = glossary_mod.build_glossary(solar, parsed.full_text)
    log.info("job %s :: glossary terms=%d", job_id, len(glossary.mapping))

    log.info("job %s :: translating elements", job_id)
    translated = translate_elements(solar, glossary, parsed.elements)

    out_docx = OUTPUT_DIR / f"{job_id}_translated.docx"
    docx_builder.build_docx(translated, out_docx, title=title or work_path.stem)
    log.info("job %s :: wrote %s", job_id, out_docx)

    out_pdf = None
    if want_pdf:
        out_pdf = docx_builder.docx_to_pdf(out_docx)
        log.info("job %s :: pdf=%s", job_id, out_pdf)

    return JSONResponse(
        {
            "job_id": job_id,
            "docx": str(out_docx),
            "pdf": str(out_pdf) if out_pdf else None,
            "glossary": glossary.mapping,
            "element_count": len(parsed.elements),
            "download_docx": f"/download/{out_docx.name}",
            "download_pdf": f"/download/{out_pdf.name}" if out_pdf else None,
        }
    )


@app.get("/download/{name}")
def download(name: str) -> FileResponse:
    target = OUTPUT_DIR / name
    if not target.exists() or ".." in name:
        raise HTTPException(404, "not found")
    return FileResponse(target, filename=name)
