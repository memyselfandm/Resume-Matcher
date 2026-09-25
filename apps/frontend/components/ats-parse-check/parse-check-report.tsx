'use client';

import type {
  ParseCheckReport,
  ParseCheckResult,
  ParseCheckSeverity,
  RoundtripFieldStatus,
} from '@/lib/api/parse-check';
import { useTranslations } from '@/lib/i18n';
import {
  SEVERITY_ORDER,
  checkMessage,
  checkTitle,
  fieldLabel,
  percent,
  type Translate,
} from '@/lib/utils/parse-check-messages';

const SEVERITY_SQUARE: Record<ParseCheckSeverity, string> = {
  fatal: 'bg-red-600',
  high: 'bg-red-600',
  medium: 'bg-orange-500',
  low: 'bg-steel-grey',
};

const FIELD_STATUS_STYLE: Record<RoundtripFieldStatus, string> = {
  found: 'border-green-700 text-green-700',
  garbled: 'border-orange-600 text-orange-700',
  missing: 'border-red-600 text-red-600',
  not_rendered: 'border-black text-ink-soft',
  hidden: 'border-black text-ink-soft',
};

function scoreSquare(score: number | null): string {
  if (score === null) return 'bg-steel-grey';
  if (score >= 80) return 'bg-green-700';
  if (score >= 60) return 'bg-orange-500';
  return 'bg-red-600';
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <h4 className="font-mono text-xs font-bold uppercase tracking-wider text-ink-soft mb-2">
      {children}
    </h4>
  );
}

function ScoreCell({
  label,
  hint,
  score,
  testId,
  t,
}: {
  label: string;
  hint: string;
  score: number | null;
  testId: string;
  t: Translate;
}) {
  return (
    <div className="bg-white p-4" data-testid={testId}>
      <div className="flex items-center gap-2">
        <div className={`w-3 h-3 ${scoreSquare(score)}`} aria-hidden="true" />
        <span className="font-mono text-xs font-bold uppercase tracking-wider">{label}</span>
      </div>
      <p className="mt-2 font-serif text-4xl font-bold tabular-nums leading-none">
        {score === null ? t('atsParseCheck.scores.notAvailable') : score}
        {score !== null && (
          <span className="font-mono text-sm font-normal text-ink-soft">
            {t('atsParseCheck.scores.outOf')}
          </span>
        )}
      </p>
      <p className="mt-2 font-sans text-xs text-ink-soft">{hint}</p>
    </div>
  );
}

export function ExpectedByTemplateBadge() {
  const { t } = useTranslations();
  return (
    <span
      className="inline-block border border-blue-700 px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-wider text-blue-700"
      title={t('atsParseCheck.expectedByTemplateHint')}
    >
      {t('atsParseCheck.expectedByTemplate')}
    </span>
  );
}

function CheckItem({
  check,
  t,
  locale,
}: {
  check: ParseCheckResult;
  t: Translate;
  locale?: string;
}) {
  const expected = check.params.expected_by_template === true;
  return (
    <li className="border-t border-black py-2 first:border-t-0" data-check-id={check.id}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-sans text-sm font-bold">{checkTitle(t, check.id)}</span>
        <span className="sr-only">{t(`atsParseCheck.status.${check.status}`)}</span>
        <span className="font-mono text-[10px] uppercase tracking-wider text-ink-soft">
          {t(`atsParseCheck.category.${check.category}`)}
        </span>
        {expected && <ExpectedByTemplateBadge />}
      </div>
      <p className="font-sans text-sm mt-0.5">{checkMessage(t, check, locale)}</p>
      {expected && (
        <p className="font-sans text-xs text-ink-soft mt-0.5">
          {t('atsParseCheck.expectedByTemplateHint')}
        </p>
      )}
    </li>
  );
}

function IssueGroups({
  checks,
  t,
  locale,
}: {
  checks: ParseCheckResult[];
  t: Translate;
  locale?: string;
}) {
  const failed = checks.filter((check) => check.status === 'fail');
  return (
    <section>
      <SectionLabel>{t('atsParseCheck.issues', { count: failed.length })}</SectionLabel>
      {failed.length === 0 ? (
        <p className="font-sans text-sm">{t('atsParseCheck.noIssues')}</p>
      ) : (
        <div className="space-y-3">
          {SEVERITY_ORDER.map((severity) => {
            const group = failed.filter((check) => check.severity === severity);
            if (group.length === 0) return null;
            return (
              <div
                key={severity}
                className="border border-black bg-white p-3"
                data-testid={`severity-${severity}`}
              >
                <div className="flex items-center gap-2 mb-1">
                  <div className={`w-3 h-3 ${SEVERITY_SQUARE[severity]}`} aria-hidden="true" />
                  <span className="font-mono text-xs font-bold uppercase tracking-wider">
                    {t(`atsParseCheck.severity.${severity}`)}
                  </span>
                </div>
                <ul>
                  {group.map((check) => (
                    <CheckItem key={check.id} check={check} t={t} locale={locale} />
                  ))}
                </ul>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function CollapsedChecks({
  label,
  checks,
  square,
  t,
  locale,
}: {
  label: string;
  checks: ParseCheckResult[];
  square: string;
  t: Translate;
  locale?: string;
}) {
  if (checks.length === 0) return null;
  return (
    <details className="border border-black bg-white">
      <summary className="cursor-pointer p-3 font-mono text-xs font-bold uppercase tracking-wider focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-700">
        <span className={`inline-block w-3 h-3 mr-2 align-middle ${square}`} aria-hidden="true" />
        {label}
      </summary>
      <ul className="border-t border-black px-3">
        {checks.map((check) => (
          <CheckItem key={check.id} check={check} t={t} locale={locale} />
        ))}
      </ul>
    </details>
  );
}

function Meter({ label, hint, value }: { label: string; hint: string; value: number }) {
  const pct = percent(value);
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="font-mono text-xs font-bold uppercase tracking-wider">{label}</span>
        <span className="font-mono text-sm font-bold tabular-nums">{pct}%</span>
      </div>
      <div
        className="mt-1 h-2 w-full border border-black bg-paper-tint"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
      >
        <div className="h-full bg-black" style={{ width: `${pct}%` }} />
      </div>
      <p className="mt-1 font-sans text-xs text-ink-soft">{hint}</p>
    </div>
  );
}

function RoundtripSection({ report, t }: { report: ParseCheckReport; t: Translate }) {
  const roundtrip = report.roundtrip;
  if (!roundtrip) return null;
  const problems = roundtrip.fields.filter(
    (field) => field.status === 'missing' || field.status === 'garbled'
  );
  const skipped = roundtrip.fields.filter(
    (field) => field.status === 'not_rendered' || field.status === 'hidden'
  ).length;
  return (
    <section className="border border-black bg-white p-4 space-y-4" data-testid="roundtrip">
      <SectionLabel>{t('atsParseCheck.roundtrip.title')}</SectionLabel>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <Meter
          label={t('atsParseCheck.roundtrip.recall')}
          hint={t('atsParseCheck.roundtrip.recallHint')}
          value={roundtrip.content_recall}
        />
        <Meter
          label={t('atsParseCheck.roundtrip.fidelity')}
          hint={t('atsParseCheck.roundtrip.fidelityHint')}
          value={roundtrip.order_fidelity}
        />
      </div>
      {problems.length === 0 ? (
        <p className="font-sans text-sm">{t('atsParseCheck.roundtrip.allFound')}</p>
      ) : (
        <div>
          <p className="font-mono text-xs font-bold uppercase tracking-wider mb-2">
            {t('atsParseCheck.roundtrip.problemFields', { count: problems.length })}
          </p>
          <ul className="space-y-1">
            {problems.map((field) => (
              <li key={field.field} className="flex flex-wrap items-center gap-2 font-sans text-sm">
                <span
                  className={`border px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-wider ${FIELD_STATUS_STYLE[field.status]}`}
                >
                  {t(`atsParseCheck.roundtrip.fieldStatus.${field.status}`)}
                </span>
                <span>{fieldLabel(t, field.field)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {skipped > 0 && (
        <p className="font-sans text-xs text-ink-soft">
          {t('atsParseCheck.roundtrip.notRenderedCount', { count: skipped })}
        </p>
      )}
      {roundtrip.truncated && (
        <p className="font-sans text-xs text-ink-soft">{t('atsParseCheck.roundtrip.truncated')}</p>
      )}
    </section>
  );
}

function ProfilesSection({ report, t }: { report: ParseCheckReport; t: Translate }) {
  if (report.profiles.length === 0) return null;
  return (
    <section className="border border-black bg-white p-4" data-testid="profiles">
      <SectionLabel>{t('atsParseCheck.profiles.title')}</SectionLabel>
      <p className="font-sans text-xs text-ink-soft mb-3">
        {t('atsParseCheck.profiles.disclaimer')}
      </p>
      <ul className="grid grid-cols-1 sm:grid-cols-3 gap-px bg-black border border-black">
        {report.profiles.map((profile) => (
          <li key={profile.id} className="bg-white p-2">
            <div className="flex items-center justify-between gap-2">
              <span className="font-sans text-sm font-bold">
                {t(`atsParseCheck.profiles.names.${profile.id}`)}
              </span>
              <span className="font-mono text-sm tabular-nums">{profile.score}</span>
            </div>
            <div className="flex items-center gap-1.5 mt-1">
              <div
                className={`w-3 h-3 ${profile.passes ? 'bg-green-700' : 'bg-red-600'}`}
                aria-hidden="true"
              />
              <span className="font-mono text-[10px] uppercase tracking-wider">
                {profile.passes
                  ? t('atsParseCheck.profiles.passes')
                  : t('atsParseCheck.profiles.atRisk')}
              </span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

interface ParseCheckReportViewProps {
  report: ParseCheckReport;
}

/** One parse-check report: scores, failing checks by severity, round trip, profiles. */
export function ParseCheckReportView({ report }: ParseCheckReportViewProps) {
  const { t, locale } = useTranslations();
  const passed = report.checks.filter((check) => check.status === 'pass');
  const notApplicable = report.checks.filter((check) => check.status === 'not_applicable');

  return (
    <div className="space-y-4" data-testid="parse-check-report">
      <div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-px bg-black border border-black">
          <ScoreCell
            label={t('atsParseCheck.scores.parseability')}
            hint={t('atsParseCheck.scores.parseabilityHint')}
            score={report.overall_score}
            testId="score-parseability"
            t={t}
          />
          <ScoreCell
            label={t('atsParseCheck.scores.content')}
            hint={t('atsParseCheck.scores.contentHint')}
            score={report.content_score}
            testId="score-content"
            t={t}
          />
        </div>
        <p className="mt-2 font-sans text-xs text-ink-soft">
          {t('atsParseCheck.scores.separateNote')}
        </p>
        <p className="mt-1 font-mono text-xs uppercase tracking-wider">
          {t('atsParseCheck.extractability.label')}:{' '}
          <span className="font-bold">
            {t(`atsParseCheck.extractability.${report.extractability}`)}
          </span>
        </p>
      </div>

      <IssueGroups checks={report.checks} t={t} locale={locale} />

      <RoundtripSection report={report} t={t} />

      <div className="space-y-2">
        <CollapsedChecks
          label={t('atsParseCheck.passedChecks', { count: passed.length })}
          checks={passed}
          square="bg-green-700"
          t={t}
          locale={locale}
        />
        <CollapsedChecks
          label={t('atsParseCheck.notApplicableChecks', { count: notApplicable.length })}
          checks={notApplicable}
          square="bg-steel-grey"
          t={t}
          locale={locale}
        />
      </div>

      <ProfilesSection report={report} t={t} />

      <details className="border border-black bg-white">
        <summary className="cursor-pointer p-3 font-mono text-xs font-bold uppercase tracking-wider focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-700">
          {t('atsParseCheck.preview.title')}
        </summary>
        <pre className="border-t border-black p-3 font-mono text-xs whitespace-pre-wrap break-words max-h-64 overflow-y-auto">
          {report.extracted_text_preview || t('atsParseCheck.preview.empty')}
        </pre>
      </details>
    </div>
  );
}
