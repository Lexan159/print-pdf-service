import io
import os
import secrets
from typing import Any

import fitz  # PyMuPDF
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError
from starlette.datastructures import UploadFile as StarletteUploadFile

app = FastAPI(title="Print PDF Service", version="1.1.0")

MM_TO_PT = 72.0 / 25.4
API_TOKEN = os.environ.get("API_TOKEN", "").strip()
MAX_PAGES = int(os.environ.get("MAX_PAGES", "100"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "30"))
MIN_DPI = float(os.environ.get("MIN_DPI", "299"))


def check_auth(authorization: str | None) -> None:
    if not API_TOKEN:
        raise HTTPException(status_code=500, detail="API_TOKEN is not configured")

    expected = f"Bearer {API_TOKEN}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def parse_float(value: Any, default: float, field_name: str) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be a number",
        ) from exc


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": "1.1.0"}


@app.post("/generate")
async def generate_pdf(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    check_auth(authorization)

    # Parse multipart manually. This avoids FastAPI/Pydantic validation issues
    # with repeated multipart file fields sent by some n8n versions.
    form = await request.form()
    uploads = form.getlist("fileInput")

    page_mm = parse_float(form.get("pageMm"), 216.0, "pageMm")
    trim_mm = parse_float(form.get("trimMm"), 210.0, "trimMm")
    bleed_mm = parse_float(form.get("bleedMm"), 3.0, "bleedMm")
    output_name = str(form.get("outputName") or "ksiazka_print.pdf")

    if not uploads:
        raise HTTPException(
            status_code=400,
            detail=(
                "No files found under multipart field 'fileInput'. "
                f"Received fields: {list(form.keys())}"
            ),
        )

    invalid_types = [
        type(upload).__name__
        for upload in uploads
        if not isinstance(upload, StarletteUploadFile)
    ]
    if invalid_types:
        raise HTTPException(
            status_code=400,
            detail=(
                "Fields named 'fileInput' were received, but they were not files. "
                f"Received types: {invalid_types}"
            ),
        )

    if len(uploads) > MAX_PAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many pages. Maximum: {MAX_PAGES}",
        )

    if page_mm <= 0 or trim_mm <= 0 or bleed_mm < 0:
        raise HTTPException(status_code=400, detail="Invalid dimensions")

    expected_page_mm = trim_mm + (2 * bleed_mm)
    if abs(page_mm - expected_page_mm) > 0.01:
        raise HTTPException(
            status_code=400,
            detail=(
                "pageMm must equal trimMm + 2 × bleedMm. "
                f"Expected {expected_page_mm:.2f} mm."
            ),
        )

    page_pt = page_mm * MM_TO_PT
    bleed_pt = bleed_mm * MM_TO_PT
    max_bytes = MAX_FILE_MB * 1024 * 1024

    pdf = fitz.open()
    minimum_detected_dpi = float("inf")

    try:
        for index, upload in enumerate(uploads, start=1):
            data = await upload.read(max_bytes + 1)

            if len(data) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Page {index} exceeds {MAX_FILE_MB} MB",
                )

            try:
                with Image.open(io.BytesIO(data)) as image:
                    width_px, height_px = image.size
                    image.verify()
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Page {index} ({upload.filename}) is not a valid image"
                    ),
                ) from exc

            ratio = width_px / height_px
            if abs(ratio - 1.0) > 0.002:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Page {index} is not square: "
                        f"{width_px} × {height_px} px"
                    ),
                )

            dpi_x = width_px / (page_mm / 25.4)
            dpi_y = height_px / (page_mm / 25.4)
            effective_dpi = min(dpi_x, dpi_y)
            minimum_detected_dpi = min(minimum_detected_dpi, effective_dpi)

            if effective_dpi < MIN_DPI:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Page {index} has only {effective_dpi:.1f} effective DPI. "
                        f"Minimum: {MIN_DPI:.1f}."
                    ),
                )

            page = pdf.new_page(width=page_pt, height=page_pt)
            media_box = page.mediabox

            page.insert_image(
                media_box,
                stream=data,
                keep_proportion=False,
                overlay=True,
            )

            page.set_bleedbox(media_box)

            trim_box = fitz.Rect(
                media_box.x0 + bleed_pt,
                media_box.y0 + bleed_pt,
                media_box.x1 - bleed_pt,
                media_box.y1 - bleed_pt,
            )
            page.set_trimbox(trim_box)

        safe_name = os.path.basename(output_name).strip() or "ksiazka_print.pdf"
        if not safe_name.lower().endswith(".pdf"):
            safe_name += ".pdf"

        pdf.set_metadata(
            {
                "title": safe_name,
                "producer": "Print PDF Service",
                "creator": "Print PDF Service",
            }
        )

        output = pdf.tobytes(
            garbage=4,
            deflate=True,
            clean=True,
        )

        return Response(
            content=output,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"',
                "X-Page-Count": str(len(uploads)),
                "X-Min-Effective-DPI": f"{minimum_detected_dpi:.2f}",
                "X-Media-Size-MM": f"{page_mm:.2f}x{page_mm:.2f}",
                "X-Trim-Size-MM": f"{trim_mm:.2f}x{trim_mm:.2f}",
                "X-Bleed-MM": f"{bleed_mm:.2f}",
            },
        )
    finally:
        pdf.close()
