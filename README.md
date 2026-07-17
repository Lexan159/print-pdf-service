# Print PDF Service

Creates a print-ready image-only PDF:

- MediaBox / BleedBox: 216 × 216 mm
- TrimBox: 210 × 210 mm
- Bleed: 3 mm on each side
- Rejects pages below 299 effective DPI
- Preserves the source image pixel dimensions

## Coolify

1. Put these files in a GitHub repository.
2. In Coolify choose **Add Resource → Application → Public/Private Repository**.
3. Use the included `Dockerfile`.
4. Expose port `8000`.
5. Add environment variables from `.env.example`.
6. Assign a domain, for example `https://pdf-api.example.com`.
7. Deploy.

Health check:

```text
GET /health
```

## n8n HTTP Request node

Method:

```text
POST
```

URL:

```text
https://pdf-api.example.com/generate
```

Header:

```text
Authorization: Bearer YOUR_API_TOKEN
```

Body Content Type:

```text
Form-Data
```

Binary parameters, all with the same Name `fileInput`:

```text
fileInput → file_0
fileInput → file_1
...
fileInput → file_19
```

Text form fields:

```text
pageMm = 216
trimMm = 210
bleedMm = 3
outputName = ksiazka_print.pdf
```

Response:

```text
Response Format = File
Put Output in Field = data
Timeout = 300000
```
