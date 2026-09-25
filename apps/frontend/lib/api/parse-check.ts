/**
 * ATS parse-check API.
 *
 * The backend returns check ids and parameters only; every user-facing string
 * is rendered from the locale files (see `lib/utils/parse-check-messages.ts`).
 */

import { apiFetch } from './client';
import type { Locale } from '@/i18n/config';
import type { TemplateSettings, TemplateType } from '@/lib/types/template-settings';

export type ParseCheckSeverity = 'fatal' | 'high' | 'medium' | 'low';
export type ParseCheckStatus = 'pass' | 'fail' | 'not_applicable';
export type ParseCheckCategory = 'extraction' | 'layout' | 'content';
export type Extractability = 'full' | 'partial' | 'none' | 'unsupported_format';
export type RoundtripFieldStatus = 'found' | 'garbled' | 'missing' | 'not_rendered' | 'hidden';
export type TemplateCheckStatus = 'ok' | 'render_failed' | 'timed_out';
export type TemplateCheckError =
  'render_busy' | 'render_timeout' | 'render_error' | 'analysis_error' | 'budget_exhausted';

export type ParseCheckParamValue = string | number | boolean | null | Array<string | number>;

export interface ParseCheckResult {
  id: string;
  category: ParseCheckCategory;
  severity: ParseCheckSeverity;
  status: ParseCheckStatus;
  params: Record<string, ParseCheckParamValue>;
  evidence: Record<string, unknown>;
}

export interface RoundtripField {
  field: string;
  status: RoundtripFieldStatus;
  score: number;
}

export interface RoundtripResult {
  content_recall: number;
  order_fidelity: number;
  fields: RoundtripField[];
  truncated: boolean;
}

export interface ProfileResult {
  id: string;
  kind: 'heuristic';
  score: number;
  passes: boolean;
}

export interface ParseCheckReport {
  schema_version: string;
  file_format: 'pdf' | 'docx' | 'doc';
  extractability: Extractability;
  content_language: string;
  overall_score: number | null;
  content_score: number | null;
  checks: ParseCheckResult[];
  roundtrip: RoundtripResult | null;
  profiles: ProfileResult[];
  extracted_text_preview: string;
}

export interface TemplateParseCheck {
  template: TemplateType;
  status: TemplateCheckStatus;
  expected_by_template: boolean;
  render_attempts: number;
  error: TemplateCheckError | null;
  report: ParseCheckReport | null;
}

export interface OwnOutputParseCheck {
  resume_id: string;
  render_locale: string;
  settings: TemplateSettings & { lang: Locale | null };
  results: TemplateParseCheck[];
}

/** Resume-content languages the parse-check endpoints accept. */
export type ParseCheckContentLanguage = 'en' | 'es' | 'fr' | 'pt' | 'de' | 'ja' | 'ko' | 'zh';

export type ParseCheckErrorKind =
  | 'busy'
  | 'timeout'
  | 'too_large'
  | 'invalid_file'
  | 'not_found'
  | 'not_ready'
  | 'renderer_unavailable'
  | 'server'
  | 'network';

/** A failed parse-check request, classified so the UI can explain it. */
export class ParseCheckError extends Error {
  readonly kind: ParseCheckErrorKind;
  readonly status: number | null;
  /** Seconds to wait before retrying (429 `Retry-After`), when known. */
  readonly retryAfterSeconds: number | null;

  constructor(
    kind: ParseCheckErrorKind,
    message: string,
    status: number | null = null,
    retryAfterSeconds: number | null = null
  ) {
    super(message);
    this.name = 'ParseCheckError';
    this.kind = kind;
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

const DEFAULT_RETRY_AFTER_SECONDS = 10;

function parseRetryAfter(value: string | null): number {
  if (!value) return DEFAULT_RETRY_AFTER_SECONDS;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds);
  const date = Date.parse(value);
  if (Number.isFinite(date)) return Math.max(0, Math.ceil((date - Date.now()) / 1000));
  return DEFAULT_RETRY_AFTER_SECONDS;
}

function kindForStatus(status: number): ParseCheckErrorKind {
  switch (status) {
    case 400:
    case 422:
      return 'invalid_file';
    case 404:
      return 'not_found';
    case 409:
      return 'not_ready';
    case 413:
      return 'too_large';
    case 503:
      return 'renderer_unavailable';
    case 504:
      return 'timeout';
    default:
      return 'server';
  }
}

async function errorFromResponse(response: Response): Promise<ParseCheckError> {
  const text = await response.text().catch(() => '');
  const message = `Parse check failed (status ${response.status}): ${text}`;
  if (response.status === 429) {
    return new ParseCheckError(
      'busy',
      message,
      429,
      parseRetryAfter(response.headers.get('Retry-After'))
    );
  }
  return new ParseCheckError(kindForStatus(response.status), message, response.status);
}

async function send(request: () => Promise<Response>): Promise<Response> {
  let response: Response;
  try {
    response = await request();
  } catch (error) {
    // Caller cancellation is not a failure the UI should report.
    if (error instanceof Error && error.name === 'AbortError') throw error;
    const message = error instanceof Error ? error.message : String(error);
    const kind = message.toLowerCase().includes('timed out') ? 'timeout' : 'network';
    throw new ParseCheckError(kind, message);
  }
  if (!response.ok) throw await errorFromResponse(response);
  return response;
}

/** Parse-check an uploaded PDF/DOCX/DOC file; nothing is stored. */
export async function parseCheckFile(
  file: File,
  options: { contentLanguage?: ParseCheckContentLanguage; signal?: AbortSignal } = {}
): Promise<ParseCheckReport> {
  const form = new FormData();
  form.append('file', file);
  if (options.contentLanguage) form.append('content_language', options.contentLanguage);
  const response = await send(() =>
    apiFetch('/ats/parse-check', { method: 'POST', body: form, signal: options.signal })
  );
  return (await response.json()) as ParseCheckReport;
}

export interface ResumeParseCheckOptions {
  settings: TemplateSettings;
  /** Render locale (localizes default section headings), as the PDF download uses. */
  lang?: Locale | null;
  allTemplates?: boolean;
  contentLanguage?: ParseCheckContentLanguage;
  signal?: AbortSignal;
}

/** Parse-check a stored resume exactly as its PDF download renders it. */
export async function parseCheckResume(
  resumeId: string,
  options: ResumeParseCheckOptions
): Promise<OwnOutputParseCheck> {
  const body = {
    settings: { ...options.settings, lang: options.lang ?? null },
    all_templates: options.allTemplates ?? false,
    ...(options.contentLanguage ? { content_language: options.contentLanguage } : {}),
  };
  const endpoint = `/resumes/${encodeURIComponent(resumeId)}/parse-check`;
  const response = await send(() =>
    apiFetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: options.signal,
    })
  );
  return (await response.json()) as OwnOutputParseCheck;
}
