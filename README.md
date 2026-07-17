# Print PDF Service v1.2

Use `POST /generate-from-urls` with JSON instead of multipart file uploads.

Example:

```json
{
  "pages": [
    {"number": 1, "url": "https://hcti.io/v1/image/example-1"},
    {"number": 2, "url": "https://hcti.io/v1/image/example-2"}
  ],
  "pageMm": 216,
  "trimMm": 210,
  "bleedMm": 3,
  "outputName": "ksiazka_print.pdf"
}
```
