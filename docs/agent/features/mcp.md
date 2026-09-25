# MCP Server

> **Model Context Protocol server that lets agents (Claude Code, Claude Desktop, any MCP client) drive Resume Matcher: upload, tailor, export and track applications.**

## Overview

The MCP server lives in `apps/backend/app/mcp/` and runs over **stdio**. Tools
reach the application through an in-process ASGI bridge (`bridge.py`): each
tool is a thin HTTP client of the same `/api/v1` routes the web UI calls, so
validation, AI time budgets, error translation and side effects (preview
claims, tracker auto-creation) are identical to the UI. No router code is
duplicated. The bridge is built on the neutral in-process client
`app/internal_client.py` (also used by the own-output parse check) and only
adds MCP tool-error mapping and upload guards.

- SDK: `mcp==2.2.0` (`MCPServer`). Protocol: **2026-07-28** (stateless
  per-request envelope, `server/discover`) and the handshake era
  **2024-11-05 ... 2025-11-25** (`initialize`) on the same stdio connection
  type; the client's first request picks the era.
- `build_mcp_server(runtime)` in `server.py` returns a fresh server per app
  lifespan (HTTP transports must not reuse a server across lifespans).
- Deprecated protocol features (Roots, Sampling, Logging) are not used. Server
  logs go to stderr; stdout carries only JSON-RPC.

## Setup (stdio)

Claude Code:

```bash
claude mcp add resume-matcher -- uv --directory <repo>/apps/backend run resume-matcher-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "resume-matcher": {
      "command": "uv",
      "args": ["--directory", "<repo>/apps/backend", "run", "resume-matcher-mcp"]
    }
  }
}
```

`python -m app.mcp` (from `apps/backend`) is equivalent. The process enters
the FastAPI lifespan, so startup migrations run and shutdown drains background
work and closes the database. It uses the same `DATA_DIR`, `.env` and
`config.json` as the backend; configure the LLM provider in the web UI or via
environment variables first.

### Requirements for PDF export

`export_resume_pdf` renders through headless Chromium, which prints the
frontend's `/print/resumes/{id}` page. That page fetches the resume
**server-side from the backend HTTP server** (`NEXT_PUBLIC_API_URL`, else
`http://127.0.0.1:8000`). With stdio you therefore need:

1. the frontend running (`FRONTEND_BASE_URL`, default `http://localhost:3000`),
2. the backend HTTP server running on the **same `DATA_DIR`** as the MCP
   process, and
3. Chromium installed (`uv run playwright install chromium`).

`get_status` checks this: it compares the local `db_instance_id` (a UUID stored
in `DATA_DIR/instance_id`, minted once the directory holds a database) with the
one `GET /api/v1/health` reports at the print page's data origin (`null` while
that backend's directory has no database yet).

| `render_path_ok` | Meaning |
|---|---|
| `true` | Both ids exist and match: the backend serving the print page uses this database. |
| `false` | No backend answered at the data origin; the ids differ; or only one side has a database. |
| `"unknown"` | The backend does not report `db_instance_id` (older version), or neither side has a database yet. |

`pdf_export_ready` is `true` only when the frontend is reachable and
`render_path_ok` is `true`.

### Configuration staleness

The backend caches `config.json` for 300 seconds (`app/config_cache.py`). The
MCP process has its own cache, so a setting changed in the web UI (content
language, feature toggles, default prompt) can take up to five minutes to
reach a running MCP server. Restart the MCP server to pick it up immediately.

## Tools

Results are JSON objects, summary first; IDs are always returned. Errors come
back as `isError: true` with the router's client-safe message (never a
traceback; unhandled router exceptions are logged server-side by the bridge).
Every id placed in a route path must match `[A-Za-z0-9_-]{1,128}` (ids are
UUIDs); anything else is rejected before a request is made, and the bridge also
refuses any route path that is not made of such segments.

| Tool | Backing route | Notes |
|---|---|---|
| `get_status` | in-process | LLM configured (no LLM call), DB counts, frontend reachability, `render_path_ok`, `pdf_export_ready`. |
| `upload_resume(path \| filename + content_base64)` | `POST /resumes/upload` | PDF/DOC/DOCX, 4 MB limit checked before sending. `path` is stdio-only. First upload becomes master. |
| `list_resumes(include_master)` | `GET /resumes/list` | |
| `get_resume(resume_id, format)` | `GET /resumes?resume_id=` | `summary` (default), `json` (full `ResumeData`), `markdown`. |
| `update_resume(resume_id, resume_data)` | `PATCH /resumes/{id}` | Full `ResumeData` replacement. |
| `set_resume_title(resume_id, title)` | `PATCH /resumes/{id}/title` | |
| `add_jobs(descriptions[], resume_id?)` | `POST /jobs/upload` | Returns `job_ids`. |
| `get_job(job_id, full)` | `GET /jobs/{id}` | Preview of the text unless `full=true`. |
| `tailor_resume_preview(resume_id, job_id, prompt_id?, wait_seconds?)` | `POST /resumes/improve/preview` | Long op. Returns `preview_id`, `summary_of_changes`, `keyword_score`. |
| `tailor_resume_confirm(preview_id, wait_seconds?)` | `POST /resumes/improve/confirm` | Long op. Returns `tailored_resume_id`, `application_id`. Idempotent: a retry returns the same result. |
| `generate_cover_letter(resume_id, wait_seconds?)` | `POST /resumes/{id}/generate-cover-letter` | Long op; tailored resumes only. |
| `generate_outreach(resume_id, wait_seconds?)` | `POST /resumes/{id}/generate-outreach` | Long op; tailored resumes only. |
| `generate_interview_prep(resume_id, wait_seconds?)` | `POST /resumes/{id}/generate-interview-prep` | Long op; tailored resumes only. |
| `export_resume_pdf(resume_id, template?, page_size?, out_path?, wait_seconds?)` | `GET /resumes/{id}/pdf` | Long op. Seven templates. `out_path` (stdio) writes a file; otherwise base64. |
| `ats_parse_check_file(path \| filename + content_base64, content_language?, detail?, wait_seconds?)` | `POST /ats/parse-check` | Long op. Can an ATS-style extractor read this file? Nothing stored. Same 4 MB / suffix guards as `upload_resume`; `path` is stdio-only. |
| `ats_parse_check_resume(resume_id, settings?, all_templates?, content_language?, render_locale?, detail?, wait_seconds?)` | `POST /resumes/{id}/parse-check` | Long op. Parse-checks the PDF a user would download. `render_locale` sets `settings.lang`. A busy server (429) becomes a tool error with a "Retry in N seconds" hint. |
| `tailor_and_verify(resume_id, job_id, template?, settings?, min_content_recall=0.95, prompt_id?, detail?, wait_seconds?)` | preview -> confirm -> `POST /resumes/{id}/parse-check` | Long op. One call: tailor, save, parse-check the saved result. See below. |
| `list_applications(status?)` | `GET /applications` | Cards grouped by column. |
| `create_application(...)` | `POST /applications` | Manual card; not needed after `tailor_resume_confirm`. |
| `update_application(application_id, ...)` | `PATCH /applications/{id}` | Only provided fields change. |
| `get_task(task_id)` / `cancel_task(task_id)` | task registry | Poll or cancel long operations. |

### ATS parse-check tools

All three are deterministic after tailoring (no LLM in the check) and return a
**summary** by default: a verdict, scores, and the failing checks rendered in
English from `app/services/ats_parse/messages_en.py`. `detail=true` adds the
full report (`report`) exactly as the REST route returns it. On the committed
render fixtures (`tests/fixtures/ats_parse/renders/`) the default outputs
measure about 0.2 KB (`ats_parse_check_file`, `ats_parse_check_resume` for one
template), 0.6 KB (`tailor_and_verify`) and 2.0 KB (`ats_parse_check_resume`
with `all_templates`); each is asserted to be under 2 KB. A check the English
catalog cannot render is reported with its raw `id` and `params` instead of a
`message`.

**Verdict (`passes`).** `content_recall >= min_content_recall` (0.95 by
default; only when a round trip was computed, so not for uploaded files) **and**
no check with `status=fail` at severity `fatal` or `high`. For two-column
templates (`swiss-two-column`, `modern-two-column`, `vivid`) the engine reports
`multi_column`/`sidebar` at `medium` with `expected_by_template: true`, so the
chosen layout never fails the verdict on its own. When `passes` is false,
`reasons` says why.

Summary shape of `tailor_and_verify` (on the committed swiss-two-column fixture
render, which recovers 0.947 of its own source; hence `min_content_recall=0.9`
in this example):

```json
{
  "status": "succeeded",
  "task_id": "3ef46a3e...",
  "tailored_resume_id": "1724da4e-...",
  "application_id": "08a37d6c-...",
  "source_resume_id": "720a6213-...",
  "job_id": "c8167d5b-...",
  "template": "swiss-two-column",
  "keyword_score": 75.0,
  "min_content_recall": 0.9,
  "passes": true,
  "parseability_score": 90,
  "content_score": 90,
  "content_recall": 0.947,
  "order_fidelity": 0.729,
  "failing_checks": [
    {
      "id": "multi_column",
      "severity": "medium",
      "message": "Multi-column layout detected. An ATS may read the columns out of order. Expected for the selected two-column template.",
      "expected_by_template": true
    }
  ]
}
```

A failing run adds `"reasons": ["content_recall 0 is below 0.95", "fatal/high
checks failed: text_layer, text_as_image"]`. `keyword_score` is the
job-keyword score from the preview (`ats_score.overall_score`); with
`detail=true` the full `keyword_score_detail` (sub-scores, missing keywords) and
the parse-check `report` are included. Preview and confirm `warnings` are
passed through when present.

`ats_parse_check_resume` returns `{resume_id, render_locale, messages, results[]}`
with one entry per template: `template`, the same verdict fields (verdict at
the 0.95 default) and `two_column_by_design: true` where applicable; an empty
`failing_checks` is omitted. `messages` maps a check id to its English message
when that message is identical for every template that fails the check (for
example `multi_column` on the three two-column templates); such entries carry
only `id`, `severity` and `expected_by_template`, while a message whose wording
differs per template (for example `section_headings`) stays inline. A template
that could not be rendered in `all_templates` mode carries
`status: render_failed | timed_out` and `error` instead of a verdict (checked
templates have no `status` field). `ats_parse_check_file` returns
`file_format`, `extractability`, `content_language` and the verdict fields.

**Persistence (`tailor_and_verify`).** The tailored resume and its tracker card
are created by the confirm step before the check runs and are **kept even
when `passes` is false** - it is a valid tailored resume; nothing is rolled
back. Fix the content (`update_resume`) or try another template
(`ats_parse_check_resume`), or delete it in the web UI. If the parse check
itself cannot run (renderer busy after three attempts that honour
`Retry-After`, renderer down), the tool fails with a message naming the saved
`tailored_resume_id` and `application_id` so the check can be retried with
`ats_parse_check_resume`.

**Idempotency (`tailor_and_verify`).** A run is keyed by the inputs that change
what gets created: `resume_id`, `job_id`, `prompt_id`, the merged template
settings, and a SHA-256 of the source resume's content (`processed_resume` and
the raw upload text) and the job description text, fetched when the call is
made. Content is hashed rather than `updated_at` because jobs have no
`updated_at`. A call with the same key while the run is in progress joins it
(same `task_id`); after it succeeded, the stored result is returned (for the
task retention of one hour) instead of tailoring again, so a lost response
never creates a second tailored resume or tracker card. Editing the source
resume or the job changes the key and starts a new run. `detail` and
`min_content_recall` are **not** part of the key: the task stores the full
result, and each call projects it for itself, recomputing `passes`/`reasons`
for its own `min_content_recall` and including `report`/`keyword_score_detail`
only with `detail=true`. `get_task` shows the view of the call that started the
run. A failed or cancelled run is not reused. Any failure after the confirm
step (renderer unavailable, an unexpected error) is reported as a tool error
that names the saved `tailored_resume_id` and `application_id`.

**Renderer.** The own-output tools need the same render path as
`export_resume_pdf` (`get_status.pdf_export_ready`). Only one own-output check
runs per process; `ats_parse_check_resume` reports a busy server as a tool
error with a retry hint, `tailor_and_verify` waits and retries.

Resources: `resume://{resume_id}` (Markdown) and `job://{job_id}` (plain
text). An unknown id returns JSON-RPC error `-32602`.

**Not exposed:** deletes, `/config/*` writes, `/config/reset` and API-key
routes. The tool-surface snapshot test
(`tests/integration/snapshots/mcp_tools.json`) fails on any change to tool
names or input schemas; regenerate it deliberately with
`uv run python -m tests.integration.test_mcp_server`.

### Caching hints (2026-07-28 clients)

`tools/list` carries `ttlMs: 3600000`, `cacheScope: "private"` (the surface
only changes with a release). `resources/read` carries `ttlMs: 0` because
resume and job content change. Handshake-era responses omit these fields.

## Previews and long operations

**Preview cache.** The confirm route needs the full previewed resume, which
the backend does not persist. `tailor_resume_preview` keeps it in the MCP
process (`previews.py`, LRU of 64, expiring with the preview's
`preview_expires_at`) under its `preview_id`. A restart, eviction or expiry
makes `tailor_resume_confirm` fail with "run tailor_resume_preview again". The
handle stays valid after a successful confirm, so retrying (for example after
a lost response) replays the router's stored confirmation: same tailored
resume, same single tracker card. A retry made while the first confirm is still
running joins that task and returns the same `task_id` instead of a conflict.
Retention note: a confirmed preview's payload stays in memory until the preview
expires (`PREVIEW_TTL_SECONDS`, default 24 hours) or is evicted by the 64-entry
LRU; restart the MCP server to drop it sooner.

**Tasks.** Long operations run in an in-memory task registry (`tasks.py`,
cancelled on shutdown). It holds at most 32 tasks, counting running ones and
finished results the client has not read yet; unread results are kept for one
hour and are never dropped early, so when every slot is taken new work is
refused with a "retry" error. Results that were already returned free their
slot. A PDF's `content_base64` is returned once (inline or by the first
`get_task` that sees the result) and then released; later reads show
`payload_released: true`, so call `export_resume_pdf` again if it is needed.
Each long tool waits up to `wait_seconds` (stdio default 200 s, maximum
1800 s) and then returns either the result with `status: "succeeded"` and a
`task_id`, or `{"status": "running", "task_id": ...}`. Poll `get_task` until
`succeeded`, `failed` or `cancelled`. While waiting, the server sends progress
heartbeats every 15 s to clients that supplied a progress token (stdio; HTTP
JSON responses cannot carry them). `cancel_task` cancels the in-flight request;
anything the route already committed is not rolled back.

Caches and tasks are process-local, which matches the single-process backend.

## Agent recipe

1. `get_status` - confirm `llm_configured`; check `pdf_export_ready` if PDFs are needed.
2. `upload_resume(path="~/cv.pdf")` - note `resume_id` (master on first upload).
3. `add_jobs(descriptions=["<job description>"])` - note `job_ids[0]`.
4. `tailor_resume_preview(resume_id, job_id)` - review `summary_of_changes` and
   `keyword_score`; if `status` is `running`, poll `get_task`.
5. `tailor_resume_confirm(preview_id)` - note `tailored_resume_id` and
   `application_id`. The tracker card already exists (status `applied`).
6. Optional: `generate_cover_letter(tailored_resume_id)`.
7. `export_resume_pdf(tailored_resume_id, template="swiss-single", out_path="~/cv-acme.pdf")`.
8. `update_application(application_id, notes="Applied via portal", applied_at="2026-09-23")`
   - update the auto-created card; do not create a second one.

### Tailor, verify, export, track

Steps 4-5 plus an ATS check in one call:

1. `tailor_and_verify(resume_id, job_id, template="swiss-single")` - if
   `status` is `running`, poll `get_task(task_id)` (or repeat the same call; it
   joins the task). Note `tailored_resume_id` and `application_id`.
2. If `passes` is false, read `reasons` and `failing_checks`: fix the content
   with `update_resume` and re-check with
   `ats_parse_check_resume(tailored_resume_id, settings={"template": ...})`, or
   compare templates with `all_templates=true`. The tailored resume already
   exists either way.
3. `export_resume_pdf(tailored_resume_id, template=<the verified template>, out_path="~/cv-acme.pdf")`.
4. `update_application(application_id, status="applied", notes="ATS check passed (recall 0.99); applied via portal")`
   on the card `tailor_and_verify` created - do not create another.

To check a file that did not come from Resume Matcher (an old CV, a
recruiter's template): `ats_parse_check_file(path="~/cv.pdf")` (stdio) or
`ats_parse_check_file(filename="cv.pdf", content_base64=...)`.

## Tests

- `tests/unit/test_mcp_components.py` - preview cache, task registry, bridge
  error mapping, upload guards, wait policy, Markdown rendering, instance id.
- `tests/unit/test_mcp_ats_summary.py` - the `passes` rule, failing-check
  rendering, per-template summaries, settings shortcuts, `Retry-After` parsing.
- `tests/integration/test_mcp_ats_tools.py` - the three ATS tools against the
  real routers with mocked LLM services and the committed real template
  renders standing in for Chromium: file and stored-resume checks, 429 and
  invalid-id mapping, `tailor_and_verify` pass/fail/two-column paths, task
  handles, idempotent retries, persistence on failure, summary size.
- `tests/integration/test_mcp_server.py` - SDK in-process client against the
  real app and an isolated database: tool snapshot, cache hints, both eras,
  full tailoring flow, error mapping, upload limits, task polling,
  `get_status` without LLM calls and the render-path probe.
- `tests/integration/test_mcp_stdio.py` - the real entry point in a
  subprocess, one process per protocol era; asserts stdout is pure JSON-RPC.
