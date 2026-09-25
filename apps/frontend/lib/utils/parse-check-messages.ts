/**
 * Localized text for ATS parse-check results.
 *
 * The backend reports check ids, statuses and parameters only. These helpers
 * turn them into sentences from the `atsParseCheck.*` locale keys, localizing
 * parameter values (section names, contact fields, verdicts) as well.
 */

import type {
  ParseCheckError,
  ParseCheckParamValue,
  ParseCheckResult,
  ParseCheckSeverity,
} from '@/lib/api/parse-check';

export type Translate = (key: string, params?: Record<string, string | number>) => string;

const PREFIX = 'atsParseCheck';

export const SEVERITY_ORDER: ParseCheckSeverity[] = ['fatal', 'high', 'medium', 'low'];

/** Reasons a check can report `not_applicable`; others fall back to a generic text. */
const NOT_APPLICABLE_REASONS = new Set([
  'no_text',
  'content_language',
  'docx',
  'pdf',
  'doc',
  'covered_by_multi_column',
  'dense_pages',
]);

/** Parameters whose values are ids that have their own localized label. */
const LOCALIZED_VALUE_PARAMS: Record<string, string> = {
  missing: 'params.sections',
  found: 'params.sections',
  fields: 'params.contactFields',
  verdict: 'params.verdict',
};

function formatNumber(value: number, locale?: string): string {
  return new Intl.NumberFormat(locale).format(value);
}

function formatParam(
  t: Translate,
  name: string,
  value: ParseCheckParamValue,
  locale?: string
): string {
  const labelPrefix = LOCALIZED_VALUE_PARAMS[name];
  const one = (item: string | number | boolean | null): string => {
    if (typeof item === 'number') return formatNumber(item, locale);
    if (labelPrefix && typeof item === 'string') {
      const key = `${PREFIX}.${labelPrefix}.${item}`;
      const label = t(key);
      return label === key ? item : label;
    }
    return String(item);
  };
  if (Array.isArray(value)) {
    if (value.length === 0) return t(`${PREFIX}.params.none`);
    return value.map(one).join(t(`${PREFIX}.params.listSeparator`));
  }
  return one(value);
}

/** The localized sentence for one check result. */
export function checkMessage(t: Translate, check: ParseCheckResult, locale?: string): string {
  if (check.status === 'not_applicable') {
    const reason = String(check.params.reason ?? '');
    return NOT_APPLICABLE_REASONS.has(reason)
      ? t(`${PREFIX}.notApplicable.${reason}`)
      : t(`${PREFIX}.notApplicable.default`);
  }
  const params: Record<string, string> = {};
  for (const [name, value] of Object.entries(check.params)) {
    params[name] = formatParam(t, name, value, locale);
  }
  const key = `${PREFIX}.checks.${check.id}.${check.status}`;
  const message = t(key, params);
  // An id this build does not know yet: show the id rather than a key path.
  return message === key ? check.id : message;
}

/** The localized check name (short title), falling back to the id. */
export function checkTitle(t: Translate, id: string): string {
  const key = `${PREFIX}.checks.${id}.title`;
  const title = t(key);
  return title === key ? id : title;
}

const ENTRY_SECTIONS = new Set(['workExperience', 'education', 'personalProjects']);
const ENTRY_FIELD = /^(workExperience|education|personalProjects)\[(\d+)\]\.(\w+)(?:\[(\d+)\])?$/;
const CUSTOM_ENTRY_FIELD = /^customSections\.[^[]+\[(\d+)\]\.(\w+)(?:\[(\d+)\])?$/;
const CUSTOM_LIST_FIELD = /^customSections\.[^[]+\.(strings)\[(\d+)\]$/;
const CUSTOM_TEXT_FIELD = /^customSections\.[^[]+\.text$/;
const ADDITIONAL_FIELD = /^additional\.(\w+)\[(\d+)\]$/;

/**
 * The field kind of a round-trip field path (`workExperience[0].title` ->
 * `workExperience.title`), matching the backend's field-kind vocabulary.
 */
export function fieldKind(path: string): string {
  if (path === 'heading.additional') return 'additional.heading';
  if (path.startsWith('heading.')) return 'heading';
  let match = ENTRY_FIELD.exec(path);
  if (match) return `${match[1]}.${match[3]}`;
  match = CUSTOM_ENTRY_FIELD.exec(path);
  if (match) return `customSections.${match[2]}`;
  if (CUSTOM_LIST_FIELD.test(path)) return 'customSections.strings';
  if (CUSTOM_TEXT_FIELD.test(path)) return 'customSections.text';
  match = ADDITIONAL_FIELD.exec(path);
  if (match) return `additional.${match[1]}`;
  return path;
}

function sectionLabel(t: Translate, key: string): string {
  const known = ENTRY_SECTIONS.has(key) || key === 'summary' || key === 'additional';
  return t(`${PREFIX}.sections.${known ? key : 'custom'}`);
}

function kindLabel(t: Translate, kind: string, fallback: string): string {
  const key = `${PREFIX}.fields.${kind}`;
  const label = t(key);
  return label === key ? fallback : label;
}

/** A readable, localized label for a round-trip field path. */
export function fieldLabel(t: Translate, path: string): string {
  const kind = fieldKind(path);
  const label = kindLabel(t, kind, path);
  if (kind === 'heading') {
    return t(`${PREFIX}.roundtrip.headingField`, {
      section: sectionLabel(t, path.slice('heading.'.length)),
    });
  }
  let match = ENTRY_FIELD.exec(path);
  if (match) {
    return t(`${PREFIX}.roundtrip.entryField`, {
      section: sectionLabel(t, match[1]),
      index: Number(match[2]) + 1,
      field: match[4] === undefined ? label : `${label} ${Number(match[4]) + 1}`,
    });
  }
  match = CUSTOM_ENTRY_FIELD.exec(path);
  if (match) {
    return t(`${PREFIX}.roundtrip.entryField`, {
      section: sectionLabel(t, 'custom'),
      index: Number(match[1]) + 1,
      field: match[3] === undefined ? label : `${label} ${Number(match[3]) + 1}`,
    });
  }
  match = CUSTOM_LIST_FIELD.exec(path) ?? ADDITIONAL_FIELD.exec(path);
  if (match) return `${label} ${Number(match[2]) + 1}`;
  return label;
}

/** The localized explanation of a failed parse-check request. */
export function parseCheckErrorMessage(t: Translate, error: ParseCheckError): string {
  return t(`${PREFIX}.errors.${error.kind}`);
}

/** Round a 0-1 ratio to a whole percentage for display. */
export function percent(ratio: number): number {
  return Number.isFinite(ratio) ? Math.round(Math.min(Math.max(ratio, 0), 1) * 100) : 0;
}
