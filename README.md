# Print PDF Service v1.3 - CMYK

This version keeps the same n8n endpoint and JSON body as v1.2, but converts
every downloaded page from RGB to ICC-managed CMYK before embedding it in PDF.

Default output:

- page / BleedBox: 214 x 214 mm
- TrimBox: 210 x 210 mm
- bleed: 2 mm per side
- minimum effective resolution: 299 DPI
- output image color space: ICCBased CMYK
- bundled print condition: FOGRA39L Coated
- no persistent image or PDF storage inside this service

The bundled profile targets the FOGRA39 / ISO Coated v2 printing condition.
You can replace `profiles/FOGRA39L_coated.icc` with the exact ICC profile
supplied by the printer and update `CMYK_PROFILE_PATH`.

## Deployment

Upload these files to the repository root:

- `main.py`
- `requirements.txt`
- `Dockerfile`
- `.env.example`
- `verify_pdf.py`
- directory `profiles/FOGRA39L_coated.icc`

Redeploy the existing Coolify application.

After deployment, `/health` should return version `1.3.0`, colorSpace `CMYK`,
and the loaded ICC profile name.

## n8n

No node changes are required. Continue using:

- POST `/generate-from-urls`
- JSON body from `Build PDF Request`
- Response Format: File

Recommended output name:

`ksiazka_print_cmyk.pdf`
