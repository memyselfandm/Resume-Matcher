'use client';

import { useId, useState } from 'react';
import ChevronDown from 'lucide-react/dist/esm/icons/chevron-down';
import ChevronUp from 'lucide-react/dist/esm/icons/chevron-up';
import { Button } from '@/components/ui/button';
import { ToggleSwitch } from '@/components/ui/toggle-switch';
import type { Locale } from '@/i18n/config';
import {
  parseCheckResume,
  type OwnOutputParseCheck,
  type TemplateParseCheck,
} from '@/lib/api/parse-check';
import type { TemplateSettings, TemplateType } from '@/lib/types/template-settings';
import { useTranslations } from '@/lib/i18n';
import { useParseCheckRequest } from '@/hooks/use-parse-check';
import { parseCheckErrorMessage, percent, type Translate } from '@/lib/utils/parse-check-messages';
import { ExpectedByTemplateBadge, ParseCheckReportView } from './parse-check-report';
import { ParseCheckStatusAlert, SelfConsistencyNote } from './parse-check-shared';

const TEMPLATE_NAME_KEYS: Record<TemplateType, string> = {
  'swiss-single': 'swissSingle',
  'swiss-two-column': 'swissTwoColumn',
  modern: 'modern',
  'modern-two-column': 'modernTwoColumn',
  latex: 'latex',
  clean: 'clean',
  vivid: 'vivid',
};

export function templateName(t: Translate, template: TemplateType): string {
  const key = TEMPLATE_NAME_KEYS[template];
  return key ? t(`builder.formatting.templates.${key}.name`) : template;
}

function StatusCell({ result, t }: { result: TemplateParseCheck; t: Translate }) {
  const square =
    result.status === 'ok'
      ? 'bg-green-700'
      : result.status === 'timed_out'
        ? 'bg-orange-500'
        : 'bg-red-600';
  return (
    <div>
      <div className="flex items-center gap-1.5">
        <div className={`w-3 h-3 shrink-0 ${square}`} aria-hidden="true" />
        <span className="font-mono text-xs font-bold uppercase tracking-wider">
          {t(`atsParseCheck.templates.statuses.${result.status}`)}
        </span>
      </div>
      {result.status !== 'ok' && result.error && (
        <p className="mt-0.5 font-sans text-xs text-ink-soft">
          {t(`atsParseCheck.templates.errors.${result.error}`)}
        </p>
      )}
    </div>
  );
}

function scoreText(t: Translate, value: number | null | undefined): string {
  return value === null || value === undefined
    ? t('atsParseCheck.scores.notAvailable')
    : String(value);
}

function ratioText(t: Translate, value: number | undefined): string {
  return value === undefined ? t('atsParseCheck.scores.notAvailable') : `${percent(value)}%`;
}

function TemplatesTable({
  results,
  selected,
  onSelect,
  t,
}: {
  results: TemplateParseCheck[];
  selected: TemplateType | null;
  onSelect: (template: TemplateType) => void;
  t: Translate;
}) {
  const header = 'p-2 text-left font-mono text-[10px] font-bold uppercase tracking-wider';
  return (
    <div className="overflow-x-auto border border-black bg-white">
      <table className="w-full min-w-[36rem] border-collapse" data-testid="templates-table">
        <caption className="sr-only">{t('atsParseCheck.templates.caption')}</caption>
        <thead className="border-b border-black bg-paper-tint">
          <tr>
            <th scope="col" className={header}>
              {t('atsParseCheck.templates.template')}
            </th>
            <th scope="col" className={header}>
              {t('atsParseCheck.templates.status')}
            </th>
            <th scope="col" className={header}>
              {t('atsParseCheck.scores.parseability')}
            </th>
            <th scope="col" className={header}>
              {t('atsParseCheck.scores.content')}
            </th>
            <th scope="col" className={header}>
              {t('atsParseCheck.roundtrip.recall')}
            </th>
            <th scope="col" className={header}>
              {t('atsParseCheck.roundtrip.fidelity')}
            </th>
            <th scope="col" className={header}>
              <span className="sr-only">{t('atsParseCheck.templates.details')}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {results.map((result) => {
            const report = result.report;
            const isSelected = selected === result.template;
            return (
              <tr
                key={result.template}
                data-template={result.template}
                className={`border-t border-black align-top ${isSelected ? 'bg-paper-tint' : ''}`}
              >
                <th scope="row" className="p-2 text-left font-sans text-sm font-bold">
                  <div>{templateName(t, result.template)}</div>
                  {result.expected_by_template && (
                    <span className="mt-1 inline-block border border-black px-1 font-mono text-[10px] uppercase tracking-wider">
                      {t('atsParseCheck.twoColumn')}
                    </span>
                  )}
                </th>
                <td className="p-2">
                  <StatusCell result={result} t={t} />
                </td>
                <td className="p-2 font-mono text-sm tabular-nums">
                  {scoreText(t, report?.overall_score)}
                </td>
                <td className="p-2 font-mono text-sm tabular-nums">
                  {scoreText(t, report?.content_score)}
                </td>
                <td className="p-2 font-mono text-sm tabular-nums">
                  {ratioText(t, report?.roundtrip?.content_recall)}
                </td>
                <td className="p-2 font-mono text-sm tabular-nums">
                  {ratioText(t, report?.roundtrip?.order_fidelity)}
                </td>
                <td className="p-2">
                  {report && (
                    <Button
                      variant="outline"
                      size="sm"
                      aria-pressed={isSelected}
                      aria-label={`${t('atsParseCheck.templates.viewDetails')}: ${templateName(t, result.template)}`}
                      onClick={() => onSelect(result.template)}
                    >
                      {isSelected
                        ? t('atsParseCheck.templates.selected')
                        : t('atsParseCheck.templates.viewDetails')}
                    </Button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TemplateResult({ result, t }: { result: TemplateParseCheck; t: Translate }) {
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-bold uppercase tracking-wider">
          {t('atsParseCheck.currentTemplate', { template: templateName(t, result.template) })}
        </span>
        {result.expected_by_template && <ExpectedByTemplateBadge />}
      </div>
      {result.report ? (
        <ParseCheckReportView report={result.report} />
      ) : (
        <StatusCell result={result} t={t} />
      )}
    </div>
  );
}

function ResultsView({ data, t }: { data: OwnOutputParseCheck; t: Translate }) {
  const firstChecked = data.results.find((result) => result.report)?.template ?? null;
  // The selection belongs to one response; a new response selects its first report.
  const [selection, setSelection] = useState<{
    data: OwnOutputParseCheck;
    template: TemplateType;
  } | null>(null);
  const selected = selection?.data === data ? selection.template : firstChecked;
  const setSelected = (template: TemplateType) => setSelection({ data, template });

  if (data.results.length === 1) return <TemplateResult result={data.results[0]} t={t} />;
  const detail = data.results.find((result) => result.template === selected);
  return (
    <div className="space-y-4">
      <div>
        <h4 className="font-mono text-xs font-bold uppercase tracking-wider text-ink-soft mb-2">
          {t('atsParseCheck.templates.title')}
        </h4>
        <TemplatesTable results={data.results} selected={selected} onSelect={setSelected} t={t} />
      </div>
      {detail && <TemplateResult result={detail} t={t} />}
    </div>
  );
}

export interface ParseCheckPanelProps {
  /** Stored resume to check; the panel explains itself and disables the check when null. */
  resumeId: string | null;
  settings: TemplateSettings;
  /** Render locale for default section headings, as the PDF download uses. */
  lang?: Locale | null;
  /** Context shown above the controls (e.g. which saved version is checked). */
  note?: string;
  defaultExpanded?: boolean;
}

/** Parse check of a stored resume as rendered with the user's template settings. */
export function ParseCheckPanel({
  resumeId,
  settings,
  lang = null,
  note,
  defaultExpanded = false,
}: ParseCheckPanelProps) {
  const { t } = useTranslations();
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [allTemplates, setAllTemplates] = useState(false);
  const request = useParseCheckRequest<OwnOutputParseCheck>();
  const contentId = useId();

  const running = request.phase === 'running';
  const coolingDown = request.retryInSeconds > 0;

  const start = () => {
    if (!resumeId) return;
    void request.run((signal) =>
      parseCheckResume(resumeId, { settings, lang, allTemplates, signal })
    );
  };

  return (
    <section
      className="border border-black bg-white shadow-sw-default"
      data-testid="parse-check-panel"
    >
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
        aria-controls={contentId}
        className="w-full flex items-center justify-between p-3 hover:bg-paper-tint focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-700"
      >
        <span className="flex items-center gap-2">
          <span className="w-2 h-2 bg-blue-700" aria-hidden="true" />
          <span className="font-mono text-xs font-bold uppercase tracking-wider">
            {t('atsParseCheck.title')}
          </span>
        </span>
        <span className="sr-only">
          {expanded ? t('atsParseCheck.hide') : t('atsParseCheck.show')}
        </span>
        {expanded ? (
          <ChevronUp className="w-4 h-4" aria-hidden="true" />
        ) : (
          <ChevronDown className="w-4 h-4" aria-hidden="true" />
        )}
      </button>

      {expanded && (
        <div id={contentId} className="border-t border-black p-4 space-y-4">
          <p className="font-sans text-sm">{t('atsParseCheck.intro')}</p>
          <SelfConsistencyNote />
          {note && <p className="font-sans text-xs text-ink-soft">{note}</p>}

          {!resumeId ? (
            <p className="font-sans text-sm">{t('atsParseCheck.noResume')}</p>
          ) : (
            <>
              <ToggleSwitch
                checked={allTemplates}
                onCheckedChange={setAllTemplates}
                label={t('atsParseCheck.allTemplates')}
                description={t('atsParseCheck.allTemplatesDescription')}
                disabled={running}
              />
              {/* Before the first result; a result names its own template. */}
              {!allTemplates && !request.data && (
                <p className="font-mono text-xs uppercase tracking-wider">
                  {t('atsParseCheck.currentTemplate', {
                    template: templateName(t, settings.template),
                  })}
                </p>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <Button onClick={start} disabled={running || coolingDown}>
                  {coolingDown
                    ? t('atsParseCheck.errors.retryIn', { seconds: request.retryInSeconds })
                    : request.data
                      ? t('atsParseCheck.rerun')
                      : t('atsParseCheck.run')}
                </Button>
                {running && (
                  <Button variant="outline" onClick={request.cancel}>
                    {t('atsParseCheck.cancel')}
                  </Button>
                )}
              </div>
            </>
          )}

          <ParseCheckStatusAlert
            running={running}
            runningMessage={
              allTemplates ? t('atsParseCheck.runningAll') : t('atsParseCheck.running')
            }
            elapsedSeconds={request.elapsedSeconds}
            error={request.error ? parseCheckErrorMessage(t, request.error) : null}
          />

          {!running && request.data && <ResultsView data={request.data} t={t} />}
          {resumeId && request.phase === 'idle' && !request.data && (
            <p className="font-sans text-sm text-ink-soft">{t('atsParseCheck.empty')}</p>
          )}
        </div>
      )}
    </section>
  );
}
