# MCP Server

> **Model Context Protocol server that lets agents (Claude Code, Claude Desktop, any MCP client) drive Resume Matcher: upload, tailor, export and track applications.**

## Overview

The MCP server lives in `apps/backend/app/mcp/` and runs over **stdio** or,
when enabled, **Streamable HTTP** at `/api/v1/mcp` on the running app. Tools
reach the application through an in-process ASGI bridge (`bridge.py`): each
tool is a thin HTTP client of the same `/api/v1` routes the web UI calls, so
validation, AI time budgets, error translation and side effects (preview
claims, tracker auto-creation) are identical to the UI. No router code is
duplicated. The bridge is built on the neutral in-process client
`app/internal_client.py` (also used by the own-output parse check) and only
adds MCP tool-error mapping and upload guards.

- SDK: `mcp==2.2.0` (`MCPServer`). Protocol: **2026-07-28** (stateless
  per-request envelope, `server/discover`) and the handshake era
  **2024-11-05 ... 2025-11-25** (`initialize`). On stdio the client's first
  request picks the era; over HTTP each request is routed by its
  `MCP-Protocol-Version` header.
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

## Setup (Streamable HTTP)

The running app serves MCP at **`/api/v1/mcp`** when `MCP_HTTP_ENABLED=1`. It
rides the existing `/api/:path*` rewrite, so in Docker (only port 3000 is
exposed) the URL is `http://localhost:3000/api/v1/mcp`; against a bare backend
it is `http://127.0.0.1:8000/api/v1/mcp`. No extra port is opened.

```bash
# .env (backend) or docker environment
MCP_HTTP_ENABLED=1
MCP_AUTH_TOKEN=$(openssl rand -hex 32)
```

Claude Code:

```bash
claude mcp add --transport http resume-matcher http://localhost:3000/api/v1/mcp \
  --header "Authorization: Bearer <MCP_AUTH_TOKEN>"
```

Other clients (`.mcp.json` style):

```json
{
  "mcpServers": {
    "resume-matcher": {
      "type": "http",
      "url": "http://localhost:3000/api/v1/mcp",
      "headers": { "Authorization": "Bearer <MCP_AUTH_TOKEN>" }
    }
  }
}
```

Transport shape (`app/mcp/http.py`):

- **Stateless JSON.** `stateless_http=True`, `json_response=True`: every POST
  gets one `application/json` response and no `Mcp-Session-Id` is issued, in
  either era. 2026-07-28 clients send `MCP-Protocol-Version: 2026-07-28` plus
  `Mcp-Method` (and `Mcp-Name` for `tools/call`, `resources/read`, ...), which
  must match the body or the request fails with HTTP 400 / `-32020`.
  Handshake-era clients `initialize` as usual; each later request is served
  by a fresh stateless transport.
- **No progress, no keepalive.** JSON responses cannot carry progress
  notifications or SSE pings. Long tools therefore wait at most
  `wait_seconds` (HTTP default **45 s**, below the ~60 s request timeout of
  typical clients; maximum `REQUEST_TIMEOUT_SECONDS - 20`, since the Next.js
  proxy aborts at `REQUEST_TIMEOUT_SECONDS`) and then return
  `{"status": "running", "task_id": ...}`. **Polling `get_task` is the
  contract** over HTTP.
- **Body limit 8 MiB** (SDK default is 4 MiB; base64 of a 4 MB upload is about
  5.6 MB). Larger bodies get HTTP 413.
- `upload_resume(path=...)` and `export_resume_pdf(out_path=...)` are refused:
  local paths are stdio-only; use `content_base64` / the returned base64.
- **Exact route, per-request lookup.** The route is registered once at import
  as `Route("/api/v1/mcp", methods=POST/GET/DELETE)`, plus the same endpoint at
  `/api/v1/mcp/` so a trailing slash is served (and authenticated) instead of
  redirected. No `Mount`, so no redirect loops through Next. Each app lifespan builds a
  fresh server and session manager and stores the manager on
  `app.state.mcp_session_manager`; the endpoint reads it per request and
  answers 404 when it is unset.
- The stdio entry point (`resume-matcher-mcp`) never starts the HTTP
  transport, whatever `MCP_HTTP_ENABLED` says.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MCP_HTTP_ENABLED` | `false` | Serve MCP at `/api/v1/mcp`. Off: the path returns 404. |
| `MCP_AUTH_TOKEN` | empty | Bearer token clients must send as `Authorization: Bearer <token>`. **Required** when enabled and at least 32 characters (`openssl rand -hex 32`): startup fails otherwise, before any data migration runs. |
| `MCP_ALLOW_NO_AUTH` | `false` | Explicit unsafe opt-out: with no token, serve unauthenticated and log a WARNING at startup and on every request. Ignored when a token is set. |
| `MCP_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*,[::1]:*` | Accepted `Host` header values (comma-separated; `host:*` = any port). Replaces the default, so an override **must still include the host of the frontend's `BACKEND_ORIGIN`** (default `127.0.0.1:*`), or every request through the Next.js proxy gets HTTP 421. |
| `MCP_ALLOWED_ORIGINS` | `http://localhost:*,http://127.0.0.1:*,http://[::1]:*` | Accepted browser `Origin` values. Requests without `Origin` (non-browser clients) pass. Otherwise HTTP 403. |
| `MCP_ALLOWED_FORWARDED_HOSTS` | empty (not checked) | Optional allow-list for `X-Forwarded-Host` (the host the client used in front of the proxy, e.g. `localhost:3000`). When set, every value of every `X-Forwarded-Host` header (comma lists included) must match, else HTTP 421; requests without the header pass. `host:*` patterns here accept only a numeric port. |

### Security notes

- **The token is the primary control.** Behind the Next.js proxy the backend
  always sees `Host: 127.0.0.1:8000` (the rewrite changes the host and adds
  `X-Forwarded-Host`), so Host checks cannot tell a local caller from a remote
  one. That is why HTTP never runs without a token unless
  `MCP_ALLOW_NO_AUTH=1` is set on purpose.
- Order of checks: disabled (404) -> bearer token (401, constant-time
  `hmac.compare_digest` on the UTF-8 bytes, before the SDK sees the request)
  -> `X-Forwarded-Host` allow-list (421) -> SDK body limit (413; a declared
  `Content-Length` is rejected before anything else in the SDK) and SDK
  Host (421) / Origin (403) validation against DNS rebinding. An oversized
  request can therefore get 413 even with a foreign Host or Origin.
- The token is held as a `SecretStr` and never logged.
- **Threat model: the token protects only `/api/v1/mcp`.** The rest of the
  REST API under `/api/v1` (which the web UI uses) is unauthenticated by
  upstream design: Resume Matcher is local-first. Setting `MCP_AUTH_TOKEN`
  does not secure the app; do not expose port 3000 (or 8000) beyond trusted
  networks regardless of it.
- Tools exposed over HTTP are the same as on stdio: no deletes, config writes
  or API-key routes. Anyone holding the token can read and edit resumes and
  spend LLM credits; treat it like a password and use HTTPS in front of any
  non-local deployment.

### Follow-up: opt-in SSE

An `MCP_HTTP_SSE=1` mode (SDK sends pings every 15 s once a request has been
silent for 15 s) would give progress and keepalives, but it is not enabled
until an end-to-end test proves the pings traverse the Next.js proxy on :3000
without buffering.

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
full report (`report`) exactly as the REST route returns it.

**Bounded by design.** Default (non-`detail`) output stays under 2 KB whatever
the report contains, because every variable part is capped
(`app/mcp/tools/ats.py`):

| Part | Cap |
|---|---|
| `failing_checks` per summary | 3 most severe (fatal, high, medium, low), then `more_failing_checks: N` |
| List params inside a message | first 5 items, then `and N more`; each item clipped to 40 characters |
| Message | 200 characters; a check the English catalog cannot render gets "No English message for this check; params: {...}" (bounded the same way) |
| Check id | 32 characters |
| `reasons` | recall reason plus at most 3 fatal/high ids, then `and N more` |
| `warnings` (`tailor_and_verify`) | first 2, each clipped to 160 characters, then `and N more` |
| `all_templates` rows | no messages or `content_score`; `top_failing_checks` = 2 `"id (severity)"` strings, then `more_failing_checks: N` |

`tests/integration/test_mcp_ats_tools.py::TestSummaryBound` feeds every tool a
synthetic worst case (every known check failing with 25-item parameter lists,
an unknown check, 20 long warnings, all seven templates) and asserts each
default output is under 2048 bytes; `detail=true` still returns every check and
the full parameters and warnings.

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

`ats_parse_check_resume` returns `{resume_id, render_locale, results[]}`. For
one template the entry is the full summary: `template`, the verdict fields
(verdict at the 0.95 default), `failing_checks` with messages, `reasons`, and
`two_column_by_design: true` where applicable. With `all_templates` each entry
is a comparison row: `template`, `two_column_by_design`, `passes`,
`parseability_score`, `content_recall`, `order_fidelity`,
`top_failing_checks` (for example `["multi_column (medium)",
"section_headings (medium)"]`) and `more_failing_checks`; check one template
(or pass `detail=true`) for the explanations. A template that could not be
rendered in `all_templates` mode carries `status: render_failed | timed_out`
and `error` instead of a verdict (checked templates have no `status` field).
`ats_parse_check_file` returns
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
1800 s; HTTP default 45 s, maximum `REQUEST_TIMEOUT_SECONDS - 20`) and then returns either the result with `status: "succeeded"` and a
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
  subprocess, one process per protocol era; asserts stdout is pure JSON-RPC
  and that HTTP settings never affect the stdio process.
- `tests/integration/test_mcp_http.py` - the HTTP transport in-process through
  the real app lifespan: 404 when disabled, startup failure without a token,
  short-token and pre-migration validation, cleanup after a failed MCP
  teardown, 401/421/403/413, trailing slash without redirect, strict
  host-pattern ports, both eras without `Mcp-Session-Id`, `-32020` header
  mismatches, a 5 MB base64 upload, two sequential `TestClient` lifespans and
  toggling `MCP_HTTP_ENABLED` without a module reload.
- `tests/integration/test_mcp_http_proxy.py` - opt-in (`e2e` marker) test
  through the Next.js proxy. Start the backend with `MCP_HTTP_ENABLED=1` and a
  token, the frontend on :3000, then run
  `MCP_E2E_BASE_URL=http://localhost:3000 MCP_E2E_TOKEN=<token> uv run pytest -m e2e tests/integration/test_mcp_http_proxy.py`.
  It needs one stored resume and an LLM that answers or hangs (a hanging one
  exercises the 45 s task-handle path); skipped without those variables.
