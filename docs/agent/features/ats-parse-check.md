# ATS Parse Check

> Can an ATS-style text extractor recover a resume's content from the file?

Parse checking measures **parseability**, not keyword fit. It complements the
keyword ATS score (`app/services/ats.py`), which is unchanged. The engine is
deterministic: no LLM is called, and the same input yields byte-identical JSON.

## Endpoint

`POST /api/v1/ats/parse-check` (multipart form)

| Field | Required | Notes |
|-------|----------|-------|
| `file` | yes | PDF, DOCX, or DOC; MIME type must match the extension; 4 MB limit |
| `content_language` | no | `en`, `es`, `fr`, `pt`, `de`, `ja`, `ko`, `zh`. Detected when omitted; weak evidence yields `unknown` |

The file is analyzed in memory and never stored. Limits: the shared document
limiter (2 concurrent conversions), a 60 s deadline covering queueing and
analysis, the 16 MB decoded-stream budget of the bounded PDF parser, at most 10
analyzed pages, and at most 200,000 extracted characters.

| Status | Meaning |
|--------|---------|
| 200 | Report returned (including `.doc`, reported as `unsupported_format`) |
| 400 | Wrong type, MIME/extension mismatch, or empty file |
| 413 | Upload over 4 MB, or decoded content over the 16 MB budget |
| 422 | Not a readable PDF/DOCX/DOC, or invalid `content_language` |
| 504 | Deadline exceeded |

## Report (`schema_version` 1.0)

```json
{
  "schema_version": "1.0",
  "file_format": "pdf",
  "extractability": "full | partial | none | unsupported_format",
  "content_language": "en",
  "overall_score": 0,
  "checks": [{"id": "multi_column", "category": "layout", "severity": "high",
              "status": "fail", "params": {"pages": [1]}, "evidence": {}}],
  "roundtrip": null,
  "profiles": [{"id": "workday", "score": 82, "passes": true}],
  "extracted_text_preview": "..."
}
```

Checks carry an `id` and `params` only, never prose. The frontend renders text
from its locale files; MCP/CLI consumers use `app/services/ats_parse/messages_en.py`.
`overall_score` starts at 100 and subtracts 100/20/10/4 per failed
fatal/high/medium/low check; any fatal failure caps it at 10. It is `null` for
`.doc`. `profiles` are heuristic re-weightings (ported from ats-screener), not
vendor-verified behavior. `roundtrip` is filled only by own-output checks.

| Check id | Category | Severity | Fails when |
|----------|----------|----------|------------|
| `file_format` | extraction | fatal | `.doc` upload (only check emitted) |
| `text_layer` | extraction | fatal | no readable text |
| `truncated` | extraction | medium | page or character cap reached |
| `unmapped_glyphs` | extraction | high | `(cid:NN)` placeholders extracted |
| `icon_font_glyphs` | extraction | medium | Private Use Area characters extracted |
| `replacement_characters` | extraction | medium | U+FFFD/control characters over 1% |
| `multi_column` | layout | high | PDF gutter band spans at least 40% of text height; DOCX section with 2+ columns |
| `sidebar` | layout | medium | PDF column narrower than 35% of page width |
| `tables` | layout | medium | DOCX tables; PDF rows of 3+ widely spaced cells |
| `images` | layout | low | images over 50 pt per side |
| `text_as_image` | extraction | high | PDF page with images and under 200 characters |
| `text_boxes` | layout | medium | DOCX text-box content |
| `header_footer_contact` | layout | high | DOCX email/phone only in header or footer |
| `page_count` | layout | low | more than 2 PDF pages |
| `contact_email` / `contact_phone` / `contact_linkedin` | content | high / medium / low | not found in extracted text |
| `section_headings` | content | medium | experience, education, or skills heading missing |
| `dates_present` | content | medium | fewer than 2 year mentions |
| `month_year_dates` | content | low | English only: years without months |
| `action_verbs` | content | low | English only: fewer than 8 distinct action verbs |
| `quantification` | content | low | fewer than 3 lines with measurable results |
| `length` | content | medium | under 150 or over 1500 words (not applicable for ja/ko/zh) |

## Engine layout

`app/services/ats_parse/`: `extract.py` (bounded pdfminer layout analysis with
pinned `LAParams`, row reconstruction, python-docx structure), `layout_checks.py`
(gutter-band column and sidebar detector, calibrated on the fixtures),
`content_checks.py` (language detection and gating), `roundtrip.py`
(self-consistency against a source payload), `profiles.py`, `report.py`,
`messages_en.py`, `engine.py`.

Fixtures in `apps/backend/tests/fixtures/ats_parse/` are synthetic and
committed. Regenerate them only when a fixture must change:

```bash
cd apps/backend
uv run python ../../scripts/generate_ats_parse_fixtures.py
```

Attribution for ported code is in `THIRD_PARTY_NOTICES.md`.
