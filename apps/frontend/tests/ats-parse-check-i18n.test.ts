import { describe, expect, it } from 'vitest';

import en from '@/messages/en.json';
import es from '@/messages/es.json';
import zh from '@/messages/zh.json';
import ja from '@/messages/ja.json';
import pt from '@/messages/pt-BR.json';
import fr from '@/messages/fr.json';
import ko from '@/messages/ko.json';
import { getNestedValue } from '@/lib/i18n/utils';
import { checkMessage, fieldKind, fieldLabel } from '@/lib/utils/parse-check-messages';
import { applyParams } from '@/lib/i18n/utils';

// Generated from the backend engine by `app.scripts.export_check_ids`; the
// backend suite fails when this file is stale, so a new check id reaches here.
import vocabulary from './fixtures/ats-parse-check-ids.json';

const LOCALES: Record<string, unknown> = { en, es, zh, ja, pt, fr, ko };

function lookup(messages: unknown, key: string): string | null {
  const value = getNestedValue(messages as Record<string, unknown>, key);
  return value === key ? null : value;
}

/** Every key the UI builds from a backend id, per vocabulary list. */
function requiredKeys(): string[] {
  return [
    ...vocabulary.check_ids.flatMap((id) => [
      `atsParseCheck.checks.${id}.title`,
      `atsParseCheck.checks.${id}.fail`,
      `atsParseCheck.checks.${id}.pass`,
    ]),
    ...vocabulary.severities.map((id) => `atsParseCheck.severity.${id}`),
    ...vocabulary.check_statuses.map((id) => `atsParseCheck.status.${id}`),
    ...vocabulary.categories.map((id) => `atsParseCheck.category.${id}`),
    ...vocabulary.extractability.map((id) => `atsParseCheck.extractability.${id}`),
    ...vocabulary.field_statuses.map((id) => `atsParseCheck.roundtrip.fieldStatus.${id}`),
    ...vocabulary.field_kinds.map((kind) => `atsParseCheck.fields.${kind}`),
    ...vocabulary.profile_ids.map((id) => `atsParseCheck.profiles.names.${id}`),
    ...vocabulary.template_statuses.map((id) => `atsParseCheck.templates.statuses.${id}`),
    ...vocabulary.template_errors.map((id) => `atsParseCheck.templates.errors.${id}`),
  ];
}

function leaves(value: unknown, prefix = ''): Array<[string, string]> {
  if (typeof value === 'string') return [[prefix, value]];
  if (!value || typeof value !== 'object') return [];
  return Object.entries(value as Record<string, unknown>).flatMap(([key, child]) =>
    leaves(child, prefix ? `${prefix}.${key}` : key)
  );
}

function placeholders(text: string): string[] {
  return [...text.matchAll(/\{([^{}]+)\}/g)].map((match) => match[1]).sort();
}

describe('ATS parse-check locale coverage', () => {
  it('reads a non-trivial backend vocabulary', () => {
    expect(vocabulary.check_ids.length).toBeGreaterThanOrEqual(20);
    expect(vocabulary.check_ids).toContain('multi_column');
    expect(vocabulary.template_ids).toHaveLength(7);
  });

  it.each(Object.keys(LOCALES))('%s has a message for every backend id', (name) => {
    const missing = requiredKeys().filter((key) => !lookup(LOCALES[name], key)?.trim());
    expect(missing, `${name} is missing parse-check keys`).toEqual([]);
  });

  it.each(Object.keys(LOCALES))('%s names every template the check can render', (name) => {
    const templateKeys: Record<string, string> = {
      'swiss-single': 'swissSingle',
      'swiss-two-column': 'swissTwoColumn',
      modern: 'modern',
      'modern-two-column': 'modernTwoColumn',
      latex: 'latex',
      clean: 'clean',
      vivid: 'vivid',
    };
    const missing = vocabulary.template_ids.filter(
      (id) => !lookup(LOCALES[name], `builder.formatting.templates.${templateKeys[id]}.name`)
    );
    expect(missing).toEqual([]);
  });

  it.each(Object.keys(LOCALES).filter((name) => name !== 'en'))(
    '%s keeps the English placeholders and is translated',
    (name) => {
      const english = new Map(leaves(en.atsParseCheck));
      const translated = leaves((LOCALES[name] as typeof en).atsParseCheck);
      const wrongPlaceholders = translated
        .filter(([key, text]) => {
          const source = english.get(key);
          return source !== undefined && placeholders(source).join() !== placeholders(text).join();
        })
        .map(([key]) => key);
      expect(wrongPlaceholders).toEqual([]);

      // Brand names, "LinkedIn" and bare placeholders legitimately match English;
      // anything beyond a small share means the section was copied untranslated.
      const identical = translated.filter(([key, text]) => english.get(key) === text);
      expect(identical.length / translated.length).toBeLessThan(0.15);
    }
  );

  it('fills every placeholder of every failure message from its backend params', () => {
    // Params each check emits (see app/services/ats_parse); a placeholder the
    // backend never fills would render as a literal "{name}".
    const params: Record<string, Record<string, unknown>> = {
      truncated: { page_limit: 10, char_limit: 100000, dense_pages: [] },
      unmapped_glyphs: { count: 1 },
      icon_font_glyphs: { count: 1 },
      replacement_characters: { count: 1 },
      text_as_image: { pages: [1] },
      sidebar: { pages: [2] },
      text_boxes: { chars: 10 },
      header_footer_contact: { fields: ['email'] },
      page_count: { pages: 3, max_pages: 2 },
      section_headings: { missing: ['skills'] },
      dates_present: { count: 1 },
      action_verbs: { count: 1, min_count: 8 },
      quantification: { count: 1, min_count: 3 },
      length: { words: 90, verdict: 'short', min_words: 150, max_words: 1500 },
    };
    for (const [name, messages] of Object.entries(LOCALES)) {
      const t = (key: string, values?: Record<string, string | number>) =>
        applyParams(getNestedValue(messages as Record<string, unknown>, key), values);
      for (const id of vocabulary.check_ids) {
        for (const status of ['pass', 'fail'] as const) {
          const text = checkMessage(t, {
            id,
            category: 'layout',
            severity: 'low',
            status,
            params: (params[id] ?? {}) as Record<string, string | number>,
            evidence: {},
          });
          expect(text, `${name} ${id}.${status}`).not.toMatch(/[{}]/);
          expect(text, `${name} ${id}.${status}`).not.toBe(id);
        }
      }
    }
  });

  it('maps every backend field kind back from its field paths', () => {
    const t = (key: string, values?: Record<string, string | number>) =>
      applyParams(getNestedValue(en as unknown as Record<string, unknown>, key), values);
    const samples: Record<string, string> = {
      'personalInfo.email': 'personalInfo.email',
      summary: 'summary',
      'heading.workExperience': 'heading',
      'heading.additional': 'additional.heading',
      'workExperience[3].years': 'workExperience.years',
      'education[0].description[1]': 'education.description',
      'personalProjects[2].role': 'personalProjects.role',
      'additional.languages[0]': 'additional.languages',
      'customSections.volunteer[0].subtitle': 'customSections.subtitle',
      'customSections.volunteer.strings[4]': 'customSections.strings',
      'customSections.volunteer.text': 'customSections.text',
    };
    for (const [path, kind] of Object.entries(samples)) {
      expect(fieldKind(path)).toBe(kind);
      expect(vocabulary.field_kinds).toContain(kind);
      expect(fieldLabel(t, path)).not.toContain('atsParseCheck');
    }
    expect(fieldLabel(t, 'customSections.volunteer[0].description[1]')).toBe(
      'Custom section 1: Bullet 2'
    );
    expect(fieldLabel(t, 'additional.languages[0]')).toBe('Language 1');
  });
});
