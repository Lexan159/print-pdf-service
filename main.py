
import io
import ipaddress
import os
import secrets
import socket
from typing import Any
from urllib.parse import urlparse

import fitz  # PyMuPDF
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

app = FastAPI(title="Print PDF Service", version="1.2.0")

MM_TO_PT = 72.0 / 25.4
API_TOKEN = os.environ.get("API_TOKEN", "").strip()
MAX_PAGES = int(os.environ.get("MAX_PAGES", "100"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "30"))
MIN_DPI = float(os.environ.get("MIN_DPI", "299"))
DOWNLOAD_TIMEOUT = float(os.environ.get("DOWNLOAD_TIMEOUT", "60"))
ALLOWED_IMAGE_HOSTS = {
    host.strip().lower()
    for host in os.environ.get("ALLOWED_IMAGE_HOSTS", "hcti.io").split(",")
    if host.strip()
}


class PageInput(BaseModel):
    number: int = Field(ge=1)
    url: str
    fileName: str | None = None


class GenerateFromUrlsRequest(BaseModel):
    pages: list[PageInput]
    pageMm: float = 216.0
    trimMm: float = 210.0
    bleedMm: float = 3.0
    outputName: str = "ksiazka_print.pdf"


def check_auth(authorization: str | None) -> None:
    if not API_TOKEN:
        raise HTTPException(status_code=500, detail="API_TOKEN is not configured")

    expected = f"Bearer {API_TOKEN}"
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def validate_dimensions(page_mm: float, trim_mm: float, bleed_mm: float) -> None:
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


def validate_remote_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)

    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Only HTTPS image URLs are allowed")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise HTTPException(status_code=400, detail="Image URL has no hostname")

    allowed = any(
        hostname == allowed_host or hostname.endswith(f".{allowed_host}")
        for allowed_host in ALLOWED_IMAGE_HOSTS
    )
    if not allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Image host '{hostname}' is not allowed",
        )

    # Block local/private destinations even if DNS is changed unexpectedly.
    try:
        addresses = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail=f"Cannot resolve image host: {hostname}") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise HTTPException(status_code=400, detail="Private network image URLs are blocked")

    return raw_url


async def download_image(client: httpx.AsyncClient, page: PageInput, max_bytes: int) -> bytes:
    url = validate_remote_url(page.url)

    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").lower()
            if content_type and not content_type.startswith("image/"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Page {page.number} URL did not return an image",
                )

            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Page {page.number} exceeds {MAX_FILE_MB} MB",
                )

            chunks: list[bytes] = []
            total = 0

            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Page {page.number} exceeds {MAX_FILE_MB} MB",
                    )
                chunks.append(chunk)

            return b"".join(chunks)

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not download page {page.number}: HTTP {exc.response.status_code}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not download page {page.number}: {exc}",
        ) from exc


def validate_image(data: bytes, page_number: int, page_mm: float) -> tuple[int, int, float]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            width_px, height_px = image.size
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Page {page_number} is not a valid image",
        ) from exc

    ratio = width_px / height_px
    if abs(ratio - 1.0) > 0.002:
        raise HTTPException(
            status_code=400,
            detail=f"Page {page_number} is not square: {width_px} × {height_px} px",
        )

    effective_dpi = min(
        width_px / (page_mm / 25.4),
        height_px / (page_mm / 25.4),
    )

    if effective_dpi < MIN_DPI:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Page {page_number} has only {effective_dpi:.1f} effective DPI. "
                f"Minimum: {MIN_DPI:.1f}."
            ),
        )

    return width_px, height_px, effective_dpi


def make_pdf(
    images: list[tuple[PageInput, bytes]],
    page_mm: float,
    trim_mm: float,
    bleed_mm: float,
    output_name: str,
) -> tuple[bytes, float]:
    page_pt = page_mm * MM_TO_PT
    bleed_pt = bleed_mm * MM_TO_PT

    pdf = fitz.open()
    minimum_detected_dpi = float("inf")

    try:
        for page_input, data in images:
            _, _, effective_dpi = validate_image(data, page_input.number, page_mm)
            minimum_detected_dpi = min(minimum_detected_dpi, effective_dpi)

            page = pdf.new_page(width=page_pt, height=page_pt)
            media_box = page.mediabox

            page.insert_image(
                media_box,
                stream=data,
                keep_proportion=False,
                overlay=True,
            )

            page.set_bleedbox(media_box)
            page.set_trimbox(
                fitz.Rect(
                    media_box.x0 + bleed_pt,
                    media_box.y0 + bleed_pt,
                    media_box.x1 - bleed_pt,
                    media_box.y1 - bleed_pt,
                )
            )

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

        return (
            pdf.tobytes(garbage=4, deflate=True, clean=True),
            minimum_detected_dpi,
        )
    finally:
        pdf.close()


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": "1.2.0",
        "allowedImageHosts": sorted(ALLOWED_IMAGE_HOSTS),
    }


@app.post("/generate-from-urls")
async def generate_from_urls(
    body: GenerateFromUrlsRequest,
    authorization: str | None = Header(default=None),
) -> Response:
    check_auth(authorization)
    validate_dimensions(body.pageMm, body.trimMm, body.bleedMm)

    if not body.pages:
        raise HTTPException(status_code=400, detail="No pages supplied")

    if len(body.pages) > MAX_PAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many pages. Maximum: {MAX_PAGES}",
        )

    pages = sorted(body.pages, key=lambda page: page.number)
    expected_numbers = list(range(1, len(pages) + 1))
    actual_numbers = [page.number for page in pages]

    if actual_numbers != expected_numbers:
        raise HTTPException(
            status_code=400,
            detail=f"Page numbering must be continuous from 1. Received: {actual_numbers}",
        )

    max_bytes = MAX_FILE_MB * 1024 * 1024
    timeout = httpx.Timeout(DOWNLOAD_TIMEOUT)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    ) as client:
        downloaded: list[tuple[PageInput, bytes]] = []

        for page in pages:
            data = await download_image(client, page, max_bytes)
            downloaded.append((page, data))

    pdf_bytes, minimum_dpi = make_pdf(
        images=downloaded,
        page_mm=body.pageMm,
        trim_mm=body.trimMm,
        bleed_mm=body.bleedMm,
        output_name=body.outputName,
    )

    safe_name = os.path.basename(body.outputName).strip() or "ksiazka_print.pdf"
    if not safe_name.lower().endswith(".pdf"):
        safe_name += ".pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Page-Count": str(len(pages)),
            "X-Min-Effective-DPI": f"{minimum_dpi:.2f}",
            "X-Media-Size-MM": f"{body.pageMm:.2f}x{body.pageMm:.2f}",
            "X-Trim-Size-MM": f"{body.trimMm:.2f}x{body.trimMm:.2f}",
            "X-Bleed-MM": f"{body.bleedMm:.2f}",
        },
    )
