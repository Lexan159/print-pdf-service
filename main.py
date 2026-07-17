import io
import os
import secrets
from typing import Annotated

import fitz  # PyMuPDF
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError

app = FastAPI(title="Print PDF Service", version="1.0.0")

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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/generate")
async def generate_pdf(
    fileInput: Annotated[list[UploadFile], File(...)],
    pageMm: Annotated[float, Form()] = 216.0,
    trimMm: Annotated[float, Form()] = 210.0,
    bleedMm: Annotated[float, Form()] = 3.0,
    outputName: Annotated[str, Form()] = "ksiazka_print.pdf",
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    check_auth(authorization)

    if not fileInput:
        raise HTTPException(status_code=400, detail="No images supplied")

    if len(fileInput) > MAX_PAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many pages. Maximum: {MAX_PAGES}",
        )

    if pageMm <= 0 or trimMm <= 0 or bleedMm < 0:
        raise HTTPException(status_code=400, detail="Invalid dimensions")

    expected_page_mm = trimMm + (2 * bleedMm)
    if abs(pageMm - expected_page_mm) > 0.01:
        raise HTTPException(
            status_code=400,
            detail=(
                f"pageMm must equal trimMm + 2 × bleedMm. "
                f"Expected {expected_page_mm:.2f} mm."
            ),
        )

    page_pt = pageMm * MM_TO_PT
    bleed_pt = bleedMm * MM_TO_PT
    target_ratio = 1.0  # current book format is square
    max_bytes = MAX_FILE_MB * 1024 * 1024

    pdf = fitz.open()
    minimum_detected_dpi = float("inf")

    try:
        for index, upload in enumerate(fileInput, start=1):
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
                    detail=f"Page {index} is not a valid image",
                ) from exc

            ratio = width_px / height_px
            if abs(ratio - target_ratio) > 0.002:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Page {index} is not square: "
                        f"{width_px} × {height_px} px"
                    ),
                )

            dpi_x = width_px / (pageMm / 25.4)
            dpi_y = height_px / (pageMm / 25.4)
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

            # Put the full-resolution image over the complete 216 × 216 mm page.
            page.insert_image(
                media_box,
                stream=data,
                keep_proportion=False,
                overlay=True,
            )

            # Full file including bleed.
            page.set_bleedbox(media_box)

            # Finished size: 210 × 210 mm, inset by 3 mm on every side.
            trim_box = fitz.Rect(
                media_box.x0 + bleed_pt,
                media_box.y0 + bleed_pt,
                media_box.x1 - bleed_pt,
                media_box.y1 - bleed_pt,
            )
            page.set_trimbox(trim_box)

        safe_name = os.path.basename(outputName).strip() or "ksiazka_print.pdf"
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
                "X-Page-Count": str(len(fileInput)),
                "X-Min-Effective-DPI": f"{minimum_detected_dpi:.2f}",
                "X-Media-Size-MM": f"{pageMm:.2f}x{pageMm:.2f}",
                "X-Trim-Size-MM": f"{trimMm:.2f}x{trimMm:.2f}",
                "X-Bleed-MM": f"{bleedMm:.2f}",
            },
        )
    finally:
        pdf.close()
