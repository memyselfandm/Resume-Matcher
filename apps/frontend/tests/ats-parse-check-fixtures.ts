import type {
  OwnOutputParseCheck,
  ParseCheckReport,
  ParseCheckResult,
  TemplateParseCheck,
} from '@/lib/api/parse-check';
import { DEFAULT_TEMPLATE_SETTINGS, type TemplateType } from '@/lib/types/template-settings';

export function check(overrides: Partial<ParseCheckResult> & { id: string }): ParseCheckResult {
  return {
    category: 'layout',
    severity: 'medium',
    status: 'pass',
    params: {},
    evidence: {},
    ...overrides,
  };
}

/** A report with a failure at every severity plus passed and not-applicable checks. */
export function makeReport(overrides: Partial<ParseCheckReport> = {}): ParseCheckReport {
  return {
    schema_version: '2.0',
    file_format: 'pdf',
    extractability: 'partial',
    content_language: 'en',
    overall_score: 42,
    content_score: 86,
    checks: [
      check({ id: 'text_layer', category: 'extraction', severity: 'fatal', status: 'fail' }),
      check({
        id: 'unmapped_glyphs',
        category: 'extraction',
        severity: 'high',
        status: 'fail',
        params: { count: 12 },
      }),
      check({
        id: 'section_headings',
        category: 'content',
        severity: 'medium',
        status: 'fail',
        params: { found: ['education'], missing: ['experience', 'skills'], render_locale: 'en' },
      }),
      check({
        id: 'page_count',
        severity: 'low',
        status: 'fail',
        params: { pages: 3, max_pages: 2 },
      }),
      check({ id: 'contact_email', category: 'content', severity: 'high', status: 'pass' }),
      check({
        id: 'text_boxes',
        status: 'not_applicable',
        params: { reason: 'pdf' },
      }),
    ],
    roundtrip: null,
    profiles: [
      { id: 'workday', kind: 'heuristic', score: 58, passes: false },
      { id: 'lever', kind: 'heuristic', score: 91, passes: true },
    ],
    extracted_text_preview: 'Ada Lovelace\nEXPERIENCE',
    ...overrides,
  };
}

export function makeTemplateResult(
  template: TemplateType,
  overrides: Partial<TemplateParseCheck> = {}
): TemplateParseCheck {
  return {
    template,
    status: 'ok',
    expected_by_template: false,
    render_attempts: 1,
    error: null,
    report: makeReport({
      overall_score: 100,
      content_score: 100,
      checks: [check({ id: 'multi_column', severity: 'high', status: 'pass' })],
      roundtrip: { content_recall: 1, order_fidelity: 0.993, fields: [], truncated: false },
    }),
    ...overrides,
  };
}

export function makeOwnOutput(results: TemplateParseCheck[]): OwnOutputParseCheck {
  return {
    resume_id: 'resume-1',
    render_locale: 'en',
    settings: { ...DEFAULT_TEMPLATE_SETTINGS, lang: 'en' },
    results,
  };
}

export function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
    ...init,
  });
}
