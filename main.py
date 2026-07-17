
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
from PIL import Image, ImageCms, UnidentifiedImageError
from pydantic import BaseModel, Field

app = FastAPI(title="Print PDF Service", version="1.3.0")

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

CMYK_PROFILE_PATH = os.environ.get(
    "CMYK_PROFILE_PATH",
    "/app/profiles/FOGRA39L_coated.icc",
)
CMYK_JPEG_QUALITY = int(os.environ.get("CMYK_JPEG_QUALITY", "100"))
CMYK_RENDERING_INTENT_NAME = os.environ.get(
    "CMYK_RENDERING_INTENT",
    "perceptual",
).strip().lower()

INTENTS = {
    "perceptual": ImageCms.Intent.PERCEPTUAL,
    "relative": ImageCms.Intent.RELATIVE_COLORIMETRIC,
    "relative_colorimetric": ImageCms.Intent.RELATIVE_COLORIMETRIC,
    "saturation": ImageCms.Intent.SATURATION,
    "absolute": ImageCms.Intent.ABSOLUTE_COLORIMETRIC,
    "absolute_colorimetric": ImageCms.Intent.ABSOLUTE_COLORIMETRIC,
}


class PageInput(BaseModel):
    number: int = Field(ge=1)
    url: str
    fileName: str | None = None


class GenerateFromUrlsRequest(BaseModel):
    pages: list[PageInput]
    pageMm: float = 214.0
    trimMm: float = 210.0
    bleedMm: float = 2.0
    outputName: str = "ksiazka_print_cmyk.pdf"


def get_output_profile() -> tuple[ImageCms.ImageCmsProfile, bytes, str]:
    if not os.path.isfile(CMYK_PROFILE_PATH):
        raise RuntimeError(
            f"CMYK profile does not exist: {CMYK_PROFILE_PATH}"
        )

    with open(CMYK_PROFILE_PATH, "rb") as profile_file:
        profile_bytes = profile_file.read()

    profile = ImageCms.ImageCmsProfile(io.BytesIO(profile_bytes))
    profile_name = ImageCms.getProfileName(profile).strip()

    return profile, profile_bytes, profile_name


OUTPUT_PROFILE, OUTPUT_PROFILE_BYTES, OUTPUT_PROFILE_NAME = get_output_profile()


def check_auth(authorization: str | None) -> None:
    if not API_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="API_TOKEN is not configured",
        )

    expected = f"Bearer {API_TOKEN}"
    if authorization is None or not secrets.compare_digest(
        authorization,
        expected,
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


def validate_dimensions(
    page_mm: float,
    trim_mm: float,
    bleed_mm: float,
) -> None:
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
        raise HTTPException(
            status_code=400,
            detail="Only HTTPS image URLs are allowed",
        )

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise HTTPException(
            status_code=400,
            detail="Image URL has no hostname",
        )

    allowed = any(
        hostname == allowed_host
        or hostname.endswith(f".{allowed_host}")
        for allowed_host in ALLOWED_IMAGE_HOSTS
    )
    if not allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Image host '{hostname}' is not allowed",
        )

    try:
        addresses = socket.getaddrinfo(
            hostname,
            parsed.port or 443,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot resolve image host: {hostname}",
        ) from exc

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
            raise HTTPException(
                status_code=400,
                detail="Private network image URLs are blocked",
            )

    return raw_url


async def download_image(
    client: httpx.AsyncClient,
    page: PageInput,
    max_bytes: int,
) -> bytes:
    url = validate_remote_url(page.url)

    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()

            content_type = response.headers.get(
                "content-type",
                "",
            ).lower()
            if (
                content_type
                and not content_type.startswith("image/")
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Page {page.number} URL did not return an image"
                    ),
                )

            content_length = response.headers.get("content-length")
            if (
                content_length
                and int(content_length) > max_bytes
            ):
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Page {page.number} exceeds "
                        f"{MAX_FILE_MB} MB"
                    ),
                )

            chunks: list[bytes] = []
            total = 0

            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Page {page.number} exceeds "
                            f"{MAX_FILE_MB} MB"
                        ),
                    )
                chunks.append(chunk)

            return b"".join(chunks)

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not download page {page.number}: "
                f"HTTP {exc.response.status_code}"
            ),
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not download page {page.number}: {exc}"
            ),
        ) from exc


def flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P"
        and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new(
            "RGBA",
            rgba.size,
            (255, 255, 255, 255),
        )
        background.alpha_composite(rgba)
        return background.convert("RGB")

    return image.convert("RGB")


def source_profile_for(image: Image.Image) -> ImageCms.ImageCmsProfile:
    embedded = image.info.get("icc_profile")
    if embedded:
        try:
            return ImageCms.ImageCmsProfile(
                io.BytesIO(embedded)
            )
        except Exception:
            pass

    return ImageCms.createProfile("sRGB")


def convert_to_cmyk_jpeg(
    source_data: bytes,
    page_number: int,
    page_mm: float,
) -> tuple[bytes, float, tuple[int, int]]:
    try:
        with Image.open(io.BytesIO(source_data)) as opened:
            width_px, height_px = opened.size
            src_profile = source_profile_for(opened)
            rgb = flatten_to_rgb(opened)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Page {page_number} is not a valid image",
        ) from exc

    if abs((width_px / height_px) - 1.0) > 0.002:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Page {page_number} is not square: "
                f"{width_px} × {height_px} px"
            ),
        )

    effective_dpi = min(
        width_px / (page_mm / 25.4),
        height_px / (page_mm / 25.4),
    )
    if effective_dpi < MIN_DPI:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Page {page_number} has only "
                f"{effective_dpi:.1f} effective DPI. "
                f"Minimum: {MIN_DPI:.1f}."
            ),
        )

    intent = INTENTS.get(CMYK_RENDERING_INTENT_NAME)
    if intent is None:
        raise HTTPException(
            status_code=500,
            detail=(
                "Invalid CMYK_RENDERING_INTENT: "
                f"{CMYK_RENDERING_INTENT_NAME}"
            ),
        )

    try:
        cmyk = ImageCms.profileToProfile(
            rgb,
            src_profile,
            OUTPUT_PROFILE,
            renderingIntent=intent,
            outputMode="CMYK",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"ICC conversion failed for page {page_number}"
            ),
        ) from exc

    output = io.BytesIO()
    cmyk.save(
        output,
        format="JPEG",
        quality=CMYK_JPEG_QUALITY,
        subsampling=0,
        optimize=False,
        progressive=False,
        icc_profile=OUTPUT_PROFILE_BYTES,
    )

    converted = output.getvalue()

    try:
        with Image.open(io.BytesIO(converted)) as check:
            if check.mode != "CMYK":
                raise ValueError(
                    f"Converted image mode is {check.mode}"
                )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"CMYK validation failed for page {page_number}"
            ),
        ) from exc

    return converted, effective_dpi, (width_px, height_px)


def validate_pdf_is_cmyk(pdf_bytes: bytes) -> None:
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page_number, page in enumerate(document, start=1):
            images = page.get_images(full=True)
            if len(images) != 1:
                raise RuntimeError(
                    f"Page {page_number} contains "
                    f"{len(images)} images instead of 1"
                )

            xref = images[0][0]
            pixmap = fitz.Pixmap(document, xref)
            color_name = (
                pixmap.colorspace.name
                if pixmap.colorspace
                else ""
            )

            if pixmap.n != 4 or "CMYK" not in color_name.upper():
                raise RuntimeError(
                    f"Page {page_number} is not CMYK: "
                    f"components={pixmap.n}, "
                    f"colorspace={color_name}"
                )
    finally:
        document.close()


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
        for page_input, source_data in images:
            cmyk_jpeg, effective_dpi, _ = (
                convert_to_cmyk_jpeg(
                    source_data,
                    page_input.number,
                    page_mm,
                )
            )
            minimum_detected_dpi = min(
                minimum_detected_dpi,
                effective_dpi,
            )

            page = pdf.new_page(
                width=page_pt,
                height=page_pt,
            )
            media_box = page.mediabox

            page.insert_image(
                media_box,
                stream=cmyk_jpeg,
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

        safe_name = (
            os.path.basename(output_name).strip()
            or "ksiazka_print_cmyk.pdf"
        )
        if not safe_name.lower().endswith(".pdf"):
            safe_name += ".pdf"

        pdf.set_metadata(
            {
                "title": safe_name,
                "producer": (
                    "Print PDF Service 1.3 - CMYK FOGRA39"
                ),
                "creator": "Print PDF Service",
                "keywords": (
                    "CMYK, FOGRA39, ISO Coated v2 condition"
                ),
            }
        )

        result = pdf.tobytes(
            garbage=4,
            deflate=True,
            clean=True,
        )
    finally:
        pdf.close()

    validate_pdf_is_cmyk(result)
    return result, minimum_detected_dpi


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": "1.3.0",
        "colorSpace": "CMYK",
        "cmykProfile": OUTPUT_PROFILE_NAME,
        "renderingIntent": CMYK_RENDERING_INTENT_NAME,
        "allowedImageHosts": sorted(ALLOWED_IMAGE_HOSTS),
        "persistentFileStorage": False,
    }


@app.post("/generate-from-urls")
async def generate_from_urls(
    body: GenerateFromUrlsRequest,
    authorization: str | None = Header(default=None),
) -> Response:
    check_auth(authorization)
    validate_dimensions(
        body.pageMm,
        body.trimMm,
        body.bleedMm,
    )

    if not body.pages:
        raise HTTPException(
            status_code=400,
            detail="No pages supplied",
        )

    if len(body.pages) > MAX_PAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many pages. Maximum: {MAX_PAGES}",
        )

    pages = sorted(
        body.pages,
        key=lambda page: page.number,
    )
    expected_numbers = list(range(1, len(pages) + 1))
    actual_numbers = [page.number for page in pages]

    if actual_numbers != expected_numbers:
        raise HTTPException(
            status_code=400,
            detail=(
                "Page numbering must be continuous from 1. "
                f"Received: {actual_numbers}"
            ),
        )

    max_bytes = MAX_FILE_MB * 1024 * 1024
    timeout = httpx.Timeout(DOWNLOAD_TIMEOUT)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
        ),
    ) as client:
        downloaded: list[tuple[PageInput, bytes]] = []

        for page in pages:
            data = await download_image(
                client,
                page,
                max_bytes,
            )
            downloaded.append((page, data))

    pdf_bytes, minimum_dpi = make_pdf(
        images=downloaded,
        page_mm=body.pageMm,
        trim_mm=body.trimMm,
        bleed_mm=body.bleedMm,
        output_name=body.outputName,
    )

    safe_name = (
        os.path.basename(body.outputName).strip()
        or "ksiazka_print_cmyk.pdf"
    )
    if not safe_name.lower().endswith(".pdf"):
        safe_name += ".pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{safe_name}"'
            ),
            "X-Page-Count": str(len(pages)),
            "X-Min-Effective-DPI": f"{minimum_dpi:.2f}",
            "X-Media-Size-MM": (
                f"{body.pageMm:.2f}x{body.pageMm:.2f}"
            ),
            "X-Trim-Size-MM": (
                f"{body.trimMm:.2f}x{body.trimMm:.2f}"
            ),
            "X-Bleed-MM": f"{body.bleedMm:.2f}",
            "X-Color-Space": "CMYK",
            "X-CMYK-Profile": OUTPUT_PROFILE_NAME,
            "X-Persistent-File-Storage": "false",
        },
    )
