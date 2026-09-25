# ATS Parse Check

> Can an ATS-style text extractor recover a resume's content from the file?

Parse checking measures **parseability**, not keyword fit. It complements the
keyword ATS score (`app/services/ats.py`), which is unchanged. The engine is
deterministic: no LLM is called, and the same input yields byte-identical JSON.

## Endpoints

- `POST /api/v1/ats/parse-check`: an uploaded file (below).
- `POST /api/v1/resumes/{resume_id}/parse-check`: Resume Matcher's own output
  (see [Own output](#own-output)).

### Uploaded file

`POST /api/v1/ats/parse-check` (multipart form)

| Field | Required | Notes |
|-------|----------|-------|
| `file` | yes | PDF, DOCX, or DOC; MIME type must match the extension; 4 MB limit |
| `content_language` | no | `en`, `es`, `fr`, `pt`, `de`, `ja`, `ko`, `zh`. Detected when omitted; weak evidence yields `unknown` |

The file is analyzed in memory and never stored. Limits:

- a dedicated single-slot limiter (separate from resume-upload conversion, so
  parse checks cannot starve uploads) and a 60 s deadline covering queueing
  and analysis; the worker thread also checks the deadline cooperatively;
- the 16 MB decoded-stream budget of the bounded PDF parser;
- at most 10 analyzed pages; DOCX body, header/footer, and text-box text are
  each capped at 200,000 characters, PDFs at the 100,000-character document
  budget below (the `truncated` check's `char_limit` param quotes the cap
  that applies to the file's format);
- per page, at most 20,000 characters or 25,000 drawing objects, checked while
  the page is interpreted and before layout analysis; per document, 100,000
  characters / 100,000 objects; pages over 600 text lines keep their text but
  skip geometric analysis. All of these are reported through `truncated`
  (`dense_pages` lists skipped pages), and extractability becomes `partial`.
  A page skipped for density never fails `text_layer`: that check becomes
  `not_applicable` (reason `dense_pages`) when no other text was recovered,
  and `overall_score` is then capped at 50, because nothing was read.
- the column detector evaluates bands only at line edges, so its cost depends
  on the number of lines (at most 600 per page), not on page coordinates.

pdfminer's hierarchical text-box grouping (`boxes_flow`) is disabled: it is
quadratic in the number of boxes, and the engine never uses box order. Text
inside form XObjects is grouped too (`all_texts`): Chromium draws
semi-transparent text (CSS `opacity`) inside one, and extractors read it.
Rotated or skewed lines (a diagonal "DRAFT" watermark, a vertical side label)
are kept in the text, each as its own row after the page's rows, but take no
part in row reconstruction or the layout checks: their bounding box spans the
page diagonally and would otherwise merge into body rows and block every
gutter band (`rotated_watermark.pdf`). Rotation is judged as displayed,
including the page's `/Rotate`: a landscape page drawn to display upright is
analyzed normally, but text that displays sideways (upright content on a
`/Rotate 90` or `270` page) is unsupported for layout analysis, since
pdfminer lays it out one glyph per line. Sheared text (synthetic italic) is
not rotated.

Word breaks: Chromium often draws words without space glyphs, so the line
text is rebuilt from glyph positions. A gap is a word break when it exceeds
the line's typical inter-glyph gap (its median, from lines with 4 or more
glyph pairs, capped at 0.25) by 0.15 of the glyph size; pdfminer's single
absolute `word_margin` read kerning gaps in capitals (`EDUCAT ION`) and CSS
letter spacing (`S U M M A R Y`) as breaks that pdftotext does not. On the
real template renders gaps inside words reach 0.11 above the line's typical
gap and the narrowest gap between words is 0.19.

Known limitations of the word-break rule:

- A line whose gaps are mostly word gaps without drawn space glyphs (spaced
  initials such as `A B C D E F`) takes those gaps as its typical gap and
  reads as one word, as pdftotext does.
- Gaps between words of about 0.12-0.15 em without a drawn space glyph merge
  the words (e.g. `1 000 000` set with narrow positioning gaps). Drawn
  U+202F or thin-space glyphs are kept.
- Lines with fewer than 4 glyph pairs use the absolute 0.15 threshold, so
  short letter-spaced text (e.g. a few letter-spaced CJK characters) can
  still split.

| Status | Meaning |
|--------|---------|
| 200 | Report returned (including `.doc`, reported as `unsupported_format`) |
| 400 | Wrong type, MIME/extension mismatch, or empty file |
| 413 | Upload over 4 MB, or decoded content over the 16 MB budget |
| 422 | Not a readable PDF/DOCX/DOC, or invalid `content_language` |
| 504 | Deadline exceeded |

## Report (`schema_version` 2.0)

```json
{
  "schema_version": "2.0",
  "file_format": "pdf",
  "extractability": "full | partial | none | unsupported_format",
  "content_language": "en",
  "overall_score": 0,
  "content_score": 0,
  "checks": [{"id": "multi_column", "category": "layout", "severity": "high",
              "status": "fail", "params": {"pages": [1]}, "evidence": {}}],
  "roundtrip": null,
  "profiles": [{"id": "workday", "kind": "heuristic", "score": 82, "passes": true}],
  "extracted_text_preview": "..."
}
```

Checks carry an `id` and `params` only, never prose. The frontend renders text
from its locale files; MCP/CLI consumers use `app/services/ats_parse/messages_en.py`.
Scores start at 100 and subtract 100/20/10/4 per failed fatal/high/medium/low
check; any fatal failure caps a score at 10.

- `overall_score` measures **parseability only**: extraction and layout checks.
  It is `null` for `.doc`.
- `content_score` scores the content checks the same way; it is `null` when no
  content check applied (no text, or `.doc`).
- `profiles` are heuristic re-weightings (ported from ats-screener, marked
  `"kind": "heuristic"`), not vendor-verified behavior.
- `roundtrip` is filled only by own-output checks.
- Own-output checks of two-column templates add `expected_by_template: true`
  to the `multi_column`/`sidebar` params and lower their severity to `medium`.

Schema history: 2.0 split `content_score` out of `overall_score` and added
`profiles[].kind`.

| Check id | Category | Severity | Fails when |
|----------|----------|----------|------------|
| `file_format` | extraction | fatal | `.doc` upload (only check emitted) |
| `text_layer` | extraction | fatal | no readable text (`not_applicable` when pages were skipped as too dense) |
| `truncated` | extraction | medium | page, character, per-page density, or document budget cap reached (`dense_pages` lists skipped pages) |
| `unmapped_glyphs` | extraction | high | `(cid:NN)` placeholders extracted |
| `icon_font_glyphs` | extraction | medium | Private Use Area characters extracted |
| `replacement_characters` | extraction | medium | U+FFFD/control characters over 1% |
| `multi_column` | layout | high | PDF gutter band spans at least 40% of text height; DOCX section with 2+ columns |
| `sidebar` | layout | medium | PDF column narrower than 35% of page width over at least 15% of the text height; `not_applicable` (`suppressed_pages`) when `multi_column` already fails on that page |
| `tables` | layout | medium | DOCX tables; PDF rows of 3+ widely spaced cells |
| `images` | layout | low | images over 50 pt per side |
| `text_as_image` | extraction | high | PDF page with images and under 200 characters |
| `text_boxes` | layout | medium | DOCX text-box content |
| `header_footer_contact` | layout | high | DOCX email/phone only in header or footer |
| `page_count` | layout | low | more than 2 PDF pages |
| `contact_email` / `contact_phone` / `contact_linkedin` | content | high / medium / low | not found in extracted text (see phone rule below) |
| `section_headings` | content | medium | experience, education, or skills heading missing |
| `dates_present` | content | medium | fewer than 2 year mentions |
| `month_year_dates` | content | low | English only: years without months |
| `action_verbs` | content | low | English only: fewer than 8 distinct action verbs |
| `quantification` | content | low | fewer than 3 lines with measurable results |
| `length` | content | medium | under 150 or over 1500 words (not applicable for ja/ko/zh) |

Phone rule: a phone number needs 10-15 digits. Short international numbers
(for example `+352 12 34 56`) are not counted. Runs made only of years and
months are dates, not phones: `2019 - 2023`, `04.2019 - 03.2021`,
`2019.04 - 2021.03`, `2019-04 - 2021-03`. A number labeled `ISBN`
(`ISBN 978-3-16-148410-0`, `ISBN-13: ...`) is a publication id, not a phone.
A phone next to a date range or an ISBN (`555-555-0100 2019 - 2023`) is
still found.

## Own output

`POST /api/v1/resumes/{resume_id}/parse-check` (JSON body, every field optional)

```json
{
  "settings": {
    "template": "swiss-single", "pageSize": "A4",
    "margins": {"top": 10, "bottom": 10, "left": 10, "right": 10},
    "spacing": {"section": 3, "item": 2, "lineHeight": 3},
    "fontSize": {"base": 3, "headerScale": 3, "headerFont": "serif", "bodyFont": "sans-serif"},
    "compactMode": false, "showContactIcons": false, "accentColor": "blue",
    "lang": null
  },
  "content_language": null,
  "all_templates": false
}
```

"Own output" is exactly the PDF `GET /api/v1/resumes/{id}/pdf` returns for
the full settings object: the frontend `TemplateSettings` (defaults equal
`DEFAULT_TEMPLATE_SETTINGS`) plus `lang`, i.e. all 17 query parameters of
that route. The endpoint fetches that PDF, and the payload the print page
renders (`GET /api/v1/resumes?resume_id=` -> `processed_resume`), through the
in-process ASGI client (`app/internal_client.py`), so validation and the
print URL are the download route's own. Nothing is stored.

- `settings.lang` is the render locale, one of the frontend locales (`en`,
  `es`, `zh`, `ja`, `pt`, `fr`, `ko`; Portuguese is `pt`, whose strings live in
  `messages/pt-BR.json`): it localizes default section headings, and the heading checks expect
  that locale's headings. `content_language` is the language of the resume
  text; it defaults to the configured content language and gates the
  English-lexicon checks. The two are independent.
- `all_templates` renders all seven templates with otherwise identical
  settings, one after another (a check never holds more than one renderer
  slot).
- Budgets: 60 s for one template, 200 s for all seven. Renderer admission is
  fail-fast (a busy renderer returns 503 instead of queueing), so a busy
  render is retried up to 3 attempts with 1 s / 2 s backoff while the budget
  allows. Other render failures are not retried. One extraction never gets
  more than the engine's 60 s, and is not started with under 1 s left.
- Renderer fairness: one own-output check runs at a time per process; another
  request gets 429 with `Retry-After: 10` instead of queueing. The check
  renders sequentially (at most one renderer slot) and marks its renders as
  background work: after a user download is refused as busy, it starts no
  render for 10 s, so a user who retries gets the slot before the next
  template. With `PDF_MAX_CONCURRENCY=1` a busy slot is a user's download, so
  instead of three attempts the check polls it every second until it frees
  (`render_attempts` counts the polls). Waiting counts against the budget;
  templates it cannot reach are `timed_out`. Known limitation: a slot held
  for long enough (or repeatedly) can make one template spend the whole sweep
  budget polling, and the later templates then report `timed_out`.

Response:

```json
{
  "resume_id": "...", "render_locale": "en", "settings": {"...": "..."},
  "results": [
    {"template": "swiss-two-column", "status": "ok", "expected_by_template": true,
     "render_attempts": 1, "error": null, "report": {"...": "ParseCheckReport with roundtrip"}},
    {"template": "modern", "status": "render_failed", "expected_by_template": false,
     "render_attempts": 3, "error": "render_busy", "report": null}
  ]
}
```

`status` is `ok`, `render_failed` (`error`: `render_busy` after all retries,
`render_timeout`, `render_error`, or `analysis_error` when the rendered PDF
cannot be read), or `timed_out` (`error`: `budget_exhausted`). With `all_templates` the response is 200 whatever the
per-template outcome.

| Status | Meaning |
|--------|---------|
| 200 | Results returned |
| 404 | Unknown resume id |
| 409 | The resume has no structured data yet (still processing, or failed) |
| 422 | Invalid settings, locale, or `content_language` (unknown fields are rejected) |
| 429 | Another own-output check is running (`Retry-After` seconds) |
| 500 | Single template: the rendered PDF could not be analyzed |
| 503 | Single template: renderer busy after retries, or render failed |
| 504 | Single template: render or analysis exceeded the budget |

**Two-column templates.** `swiss-two-column`, `modern-two-column`, and
`vivid` are two-column by design (CSS grids of 65:35, 65:35, and 63:37).
For them `expected_by_template` is `true`, and their `multi_column` and
`sidebar` checks carry `params.expected_by_template: true` with severity
lowered to `medium`: the signal is still reported (an ATS may read the
columns out of order), but it is the chosen design. `swiss-single`, `modern`,
`latex`, and `clean` are single-column (full-width blocks; dates and
locations right-aligned with flex `justify-between`).

### Round trip

`report.roundtrip` compares the extracted text with the rendered payload
(`app/services/ats_parse/roundtrip.py`), after localizing default section
names the way the print page does.

- `content_recall`: share of expected fields `found` (order-free). Short
  fields (1-2 tokens) must match exactly; longer ones need 90% token coverage
  (50% or more is `garbled`, less is `missing`).
- `order_fidelity`: Kendall tau over found fields, rescaled to [0, 1], against
  the template's render order.
- Fields in hidden sections are `hidden`; fields a template does not print are
  `not_rendered`. Neither counts against recall.
- Custom sections follow the templates' rules: one is printed only when its
  `sectionMeta` entry has a falsy `isDefault`, and then only the content of
  its meta `sectionType` (items, strings, or text), with its heading only when
  that content is non-empty. Anything else is `not_rendered`. Note that the
  backend `SectionMeta` model defaults `isDefault` to `true`, so a custom
  section saved without the flag is silently left out of the PDF.
- At most 1,000 expected fields are compared (`roundtrip.truncated` is then
  `true`), and the check's deadline is enforced per field, so a huge payload
  cannot run past the budget.

`app/services/ats_parse/templates.py` holds each template's rendered-field
map and render order, mirroring `apps/frontend/components/resume/`: every
template prints every personal, entry, and additional-list field; header
contact order differs (latex and clean start with the location, vivid with
the links); the two-column templates place sections by a fixed layout (main
column, then sidebar) instead of `sectionMeta` order, and replace the
"additional" section's name with fixed per-list headings, so that heading is
`not_rendered` there. Tests keep the template ids and localized default
headings in sync with the frontend files.

Entries (jobs, schools, projects, custom items) are anchored by walking the
text in render order: an entry starts at the earliest verbatim occurrence of
one of its identity fields (title, company, degree, institution, project
name, role) that no earlier entry's bullets claim, and every value of an
entry must be found inside that entry's span. A bullet that mentions the next
entry's employer ("Shipped features for Google Maps") therefore stays in its
own entry, and a value present only in an identical duplicate entry is not
reported as found. Limitation: duplicate entries are told apart only when
they have at least two identity values.

### Results on real renders

The committed fixtures in `tests/fixtures/ats_parse/renders/` (`renders.tar.xz`
plus `source.json` and `manifest.json`) were rendered through the real route
in the production Docker image (Linux, Chromium 145.0.7632.6 headless shell,
DejaVu fonts; the manifest records the image ID), with default settings, from
a synthetic resume with a repeated employer, a bullet naming that employer,
HTML bullets, a visible and a hidden custom section:

| Template | multi_column | sidebar | expected_by_template | content_recall | order_fidelity | overall | content |
|----------|--------------|---------|----------------------|----------------|----------------|---------|---------|
| swiss-single | pass | pass | false | 1.000 | 0.993 | 100 | 100 |
| swiss-two-column | fail | not_applicable | true | 0.947 | 0.729 | 90 | 90 |
| modern | pass | pass | false | 1.000 | 0.993 | 100 | 100 |
| modern-two-column | fail | not_applicable | true | 0.947 | 0.734 | 90 | 90 |
| latex | pass | pass | false | 1.000 | 0.991 | 100 | 100 |
| clean | pass | pass | false | 1.000 | 0.996 | 100 | 100 |
| vivid | fail | pass | true | 0.947 | 0.720 | 90 | 90 |

Column detector re-validation (on these renders and on earlier macOS
renders): no single-column render yields any gutter candidate at band widths
from 6 to 18 pt; the two-column renders have a gutter spanning 0.90-0.92 of
the text height (threshold 0.40) at every width, so the frozen 9 pt / 40%
thresholds hold with a wide margin. The sidebar is reported under
`multi_column` (swiss/modern two-column: 0.33 of page width, suppressed) or
not narrow enough to count separately (vivid: 0.35).

Extraction agreement with poppler's `pdftotext` (whitespace tokens after NFKC
and case folding, multiset F1): 0.998 on the eight Linux renders, 0.991 on
earlier macOS renders (the difference is the U+F765 glyphs below), and 1.000
on the synthetic PDF fixtures with an ordinary text layer
(`balanced_two_column`, `clean_single_column`, `icon_font`, `spanish`,
`swiss_single_like`, `two_column`). `cid_glyphs`, `rotated_watermark`, and
`twelve_pages` differ from pdftotext by design (unmapped CID glyphs are
reported as `(cid:N)`, rotated text is kept as its own row, and pages past the
10-page cap are not read); `image_only` and `decompression_bomb` have no
comparable text. The remaining Linux differences are vivid headings that
pdftotext itself splits (`E XPERIENCE`).

Findings on these renders (reported, not suppressed):

- In the two-column templates the extractor reads rows across both columns,
  so sidebar text interleaves with wrapped main-column lines (the summary and
  some sidebar entries are `garbled`, order fidelity 0.72-0.73), and sidebar
  headings share rows with main-column text, so `section_headings` misses
  one of them.
- The contact icons (`showContactIcons`) are lucide-react components, i.e.
  inline SVG, which extracts no text; a unit test keeps every template on
  them, so they cannot trip `icon_font_glyphs`.

macOS renders (system fonts) differ: clean and vivid set job titles in
`font-variant: small-caps`, and with the macOS system font Chromium emits the
small-cap "e" as U+F765 (Private Use Area), so `icon_font_glyphs` fails and
those titles are `missing` (clean recall 0.90, vivid 0.83); the DejaVu fonts
have no small caps, so Chromium synthesizes them from capitals and the Linux
image is unaffected. Tests that depend on the renderer's fonts key on the
manifest's `platform`.

Synthetic italic: the image ships no italic faces (DejaVu Sans, Serif and
Mono in Book and Bold, Noto CJK), so Chromium slants italic text with a
sheared text matrix (latex's job titles, `<em>` in bullets). Shear is not
rotation, so that text stays in its rows.

Regenerate the fixtures when a template changes, in the production image; see
the docstring of `scripts/generate_ats_render_fixtures.py` (`--base-url`).
The same flow runs end to end against a local frontend and Chromium as an
opt-in test: `uv run pytest -m pdf`.

## Scope and caveats

- The extractor model is **line-based** (pdfminer lines rebuilt into visual
  rows, like ats-screener's pdf.js reconstruction). It is representative, not
  any vendor's parser.
- The upload fixtures imitate the templates' geometry in synthetic HTML;
  the own-output fixtures are real template renders, and the detector
  thresholds were re-validated on them (see above).
- A second extractor (pypdf) and the `extractor_disagreement` check are
  deferred until maintainers agree to make pypdf a runtime dependency.

## Engine layout

`app/services/ats_parse/`: `extract.py` (bounded pdfminer layout analysis with
pinned `LAParams`, row reconstruction, python-docx structure), `layout_checks.py`
(gutter-band column and sidebar detector, calibrated on the fixtures),
`content_checks.py` (language detection and gating), `roundtrip.py`
(self-consistency against a source payload), `templates.py` (rendered-field
maps, render order, localized default headings), `own_output.py` (render
through the internal client, retries, budgets, fairness), `profiles.py`,
`report.py`, `messages_en.py`, `engine.py`.

Fixtures in `apps/backend/tests/fixtures/ats_parse/` are synthetic and
committed. Regenerate them only when a fixture must change:

```bash
cd apps/backend
uv run python ../../scripts/generate_ats_parse_fixtures.py
```

Attribution for ported code is in `THIRD_PARTY_NOTICES.md`.
