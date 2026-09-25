import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ParseCheckPanel } from '@/components/ats-parse-check/parse-check-panel';
import { ParseCheckReportView } from '@/components/ats-parse-check/parse-check-report';
import { DEFAULT_TEMPLATE_SETTINGS } from '@/lib/types/template-settings';
import { translate } from '@/lib/i18n/server';
import {
  check,
  jsonResponse,
  makeOwnOutput,
  makeReport,
  makeTemplateResult,
} from './ats-parse-check-fixtures';

// Real English strings, so the assertions cover the locale keys the UI uses.
vi.mock('@/lib/i18n', () => ({
  useTranslations: () => ({
    t: (key: string, params?: Record<string, string | number>) => translate('en', key, params),
    locale: 'en',
  }),
}));

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderPanel(props: Partial<React.ComponentProps<typeof ParseCheckPanel>> = {}) {
  return render(
    <ParseCheckPanel
      resumeId="resume-1"
      settings={DEFAULT_TEMPLATE_SETTINGS}
      lang="es"
      defaultExpanded
      {...props}
    />
  );
}

function lastRequestBody(): Record<string, unknown> {
  const [, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit];
  return JSON.parse(String(init.body));
}

describe('ParseCheckReportView', () => {
  it('groups failing checks by severity with localized, parameterized messages', () => {
    render(<ParseCheckReportView report={makeReport()} />);

    const fatal = screen.getByTestId('severity-fatal');
    expect(within(fatal).getByText('Fatal')).toBeInTheDocument();
    expect(within(fatal).getByText(/No selectable text was found/)).toBeInTheDocument();

    const high = screen.getByTestId('severity-high');
    expect(
      within(high).getByText(/12 characters were extracted as \(cid:NN\)/)
    ).toBeInTheDocument();

    const medium = screen.getByTestId('severity-medium');
    // Section ids in params are localized, not shown raw.
    expect(
      within(medium).getByText('Standard section headings not found: Experience, Skills.')
    ).toBeInTheDocument();

    const low = screen.getByTestId('severity-low');
    expect(
      within(low).getByText('The resume has 3 pages; most systems prefer 2 or fewer.')
    ).toBeInTheDocument();

    expect(screen.getByText('Issues (4)')).toBeInTheDocument();
    // Passing checks never appear among the issues.
    expect(within(high).queryByText('Email address found.')).not.toBeInTheDocument();
  });

  it('lists passed and not-applicable checks separately', () => {
    render(<ParseCheckReportView report={makeReport()} />);
    expect(screen.getByText('Passed checks (1)')).toBeInTheDocument();
    expect(screen.getByText('Email address found.')).toBeInTheDocument();
    expect(screen.getByText('Not applicable (1)')).toBeInTheDocument();
    expect(screen.getByText('Applies to DOCX files only.')).toBeInTheDocument();
  });

  it('labels the parseability and content scores separately', () => {
    render(<ParseCheckReportView report={makeReport()} />);
    const parseability = screen.getByTestId('score-parseability');
    expect(within(parseability).getByText('Parseability')).toBeInTheDocument();
    expect(parseability).toHaveTextContent('42/100');
    const content = screen.getByTestId('score-content');
    expect(within(content).getByText('Content')).toBeInTheDocument();
    expect(content).toHaveTextContent('86/100');
    expect(screen.getByText('Partial')).toBeInTheDocument();
  });

  it('shows N/A when a score does not apply', () => {
    render(<ParseCheckReportView report={makeReport({ content_score: null })} />);
    expect(screen.getByTestId('score-content')).toHaveTextContent('N/A');
  });

  it('shows an empty state when nothing failed', () => {
    render(
      <ParseCheckReportView
        report={makeReport({ checks: [check({ id: 'tables', status: 'pass' })] })}
      />
    );
    expect(screen.getByText('No issues found.')).toBeInTheDocument();
  });

  it('marks heuristic profiles as approximations', () => {
    render(<ParseCheckReportView report={makeReport()} />);
    const profiles = screen.getByTestId('profiles');
    expect(within(profiles).getByText('Heuristic ATS profiles')).toBeInTheDocument();
    expect(within(profiles).getByText(/Not vendor-verified/)).toBeInTheDocument();
    expect(within(profiles).getByText('Workday')).toBeInTheDocument();
    expect(within(profiles).getByText('At risk')).toBeInTheDocument();
    expect(within(profiles).getByText('Likely parses')).toBeInTheDocument();
  });

  it('reports round-trip recall, fidelity, and missing or garbled fields', () => {
    render(
      <ParseCheckReportView
        report={makeReport({
          roundtrip: {
            content_recall: 0.947,
            order_fidelity: 0.729,
            truncated: false,
            fields: [
              { field: 'personalInfo.name', status: 'found', score: 1 },
              { field: 'workExperience[1].company', status: 'missing', score: 0 },
              { field: 'workExperience[0].description[2]', status: 'garbled', score: 0.6 },
              { field: 'heading.education', status: 'missing', score: 0 },
              { field: 'additional.heading', status: 'not_rendered', score: 0 },
              { field: 'customSections.volunteer.text', status: 'hidden', score: 0 },
            ],
          },
        })}
      />
    );
    const roundtrip = screen.getByTestId('roundtrip');
    expect(within(roundtrip).getByRole('meter', { name: 'Content recall' })).toHaveAttribute(
      'aria-valuenow',
      '95'
    );
    expect(within(roundtrip).getByRole('meter', { name: 'Order fidelity' })).toHaveAttribute(
      'aria-valuenow',
      '73'
    );
    expect(within(roundtrip).getByText('Missing or garbled fields (3)')).toBeInTheDocument();
    expect(within(roundtrip).getByText('Experience 2: Company')).toBeInTheDocument();
    expect(within(roundtrip).getByText('Experience 1: Bullet 3')).toBeInTheDocument();
    expect(within(roundtrip).getByText('Heading: Education')).toBeInTheDocument();
    expect(within(roundtrip).getAllByText('Missing')).toHaveLength(2);
    expect(within(roundtrip).getByText('Garbled')).toBeInTheDocument();
    // not_rendered / hidden fields are explained, not listed as problems.
    expect(within(roundtrip).getByText(/2 fields are not printed/)).toBeInTheDocument();
  });
});

describe('ParseCheckPanel', () => {
  it('checks the current template with the user settings and render locale', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        makeOwnOutput([
          makeTemplateResult('swiss-two-column', {
            expected_by_template: true,
            report: makeReport({
              checks: [
                check({
                  id: 'multi_column',
                  severity: 'medium',
                  status: 'fail',
                  params: { pages: [1], expected_by_template: true },
                }),
              ],
            }),
          }),
        ])
      )
    );
    renderPanel({ settings: { ...DEFAULT_TEMPLATE_SETTINGS, template: 'swiss-two-column' } });

    expect(screen.getByText(/Self-consistency check/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Run parse check' }));

    const report = await screen.findByTestId('parse-check-report');
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe('/api/v1/resumes/resume-1/parse-check');
    expect(lastRequestBody()).toMatchObject({
      all_templates: false,
      settings: { template: 'swiss-two-column', lang: 'es', pageSize: 'A4' },
    });
    // The badge appears for the template and on the expected check.
    expect(screen.getAllByText('Expected for this template').length).toBeGreaterThanOrEqual(2);
    expect(within(report).getByText(/Multi-column layout detected/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled();
  });

  it('waits out Retry-After when another check is running (429)', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response('{"detail":"busy"}', { status: 429, headers: { 'Retry-After': '1' } })
    );
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Run parse check' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Another parse check of rendered output is running.'
    );
    const retry = screen.getByRole('button', { name: 'Try again in 1s' });
    expect(retry).toBeDisabled();

    // The button re-enables once the Retry-After interval has passed.
    const run = await screen.findByRole('button', { name: 'Run parse check' }, { timeout: 3000 });
    expect(run).toBeEnabled();
    fetchMock.mockResolvedValueOnce(jsonResponse(makeOwnOutput([makeTemplateResult('modern')])));
    fireEvent.click(run);
    expect(await screen.findByTestId('parse-check-report')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each([
    [504, 'The parse check timed out.'],
    [503, 'The PDF renderer is unavailable or busy.'],
    [409, 'This resume is still being processed.'],
    [404, 'This resume no longer exists.'],
    [500, 'The parse check failed. Please try again.'],
  ])('explains HTTP %i', async (status, message) => {
    fetchMock.mockResolvedValueOnce(new Response('{"detail":"x"}', { status }));
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Run parse check' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(screen.getByRole('button', { name: 'Run parse check' })).toBeEnabled();
  });

  it('explains a network failure', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Run parse check' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the server.');
  });

  it('shows progress while running and can cancel', async () => {
    fetchMock.mockImplementationOnce(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () =>
            reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
          );
        })
    );
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Run parse check' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Checking…');
    expect(screen.getByRole('button', { name: 'Run parse check' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel check' }));
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run parse check' })).toBeEnabled();
  });

  it('compares all templates, including render_failed and timed_out rows', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        makeOwnOutput([
          makeTemplateResult('swiss-single'),
          makeTemplateResult('swiss-two-column', {
            expected_by_template: true,
            report: makeReport({
              overall_score: 90,
              content_score: 88,
              roundtrip: {
                content_recall: 0.947,
                order_fidelity: 0.729,
                fields: [],
                truncated: false,
              },
            }),
          }),
          makeTemplateResult('modern', {
            status: 'render_failed',
            error: 'render_busy',
            render_attempts: 3,
            report: null,
          }),
          makeTemplateResult('modern-two-column', { expected_by_template: true }),
          makeTemplateResult('latex'),
          makeTemplateResult('clean'),
          makeTemplateResult('vivid', {
            status: 'timed_out',
            error: 'budget_exhausted',
            expected_by_template: true,
            report: null,
          }),
        ])
      )
    );
    renderPanel();
    fireEvent.click(screen.getByRole('switch', { name: 'Check all templates' }));
    fireEvent.click(screen.getByRole('button', { name: 'Run parse check' }));

    const table = await screen.findByTestId('templates-table');
    expect(lastRequestBody()).toMatchObject({ all_templates: true });
    expect(within(table).getAllByRole('row')).toHaveLength(8);

    const failed = table.querySelector('tr[data-template="modern"]') as HTMLElement;
    expect(within(failed).getByText('Render failed')).toBeInTheDocument();
    expect(within(failed).getByText(/renderer stayed busy/)).toBeInTheDocument();
    expect(within(failed).queryByRole('button')).not.toBeInTheDocument();

    const timedOut = table.querySelector('tr[data-template="vivid"]') as HTMLElement;
    expect(within(timedOut).getByText('Timed out')).toBeInTheDocument();
    expect(within(timedOut).getByText('Two-column')).toBeInTheDocument();

    const twoColumn = table.querySelector('tr[data-template="swiss-two-column"]') as HTMLElement;
    expect(twoColumn).toHaveTextContent('90');
    expect(twoColumn).toHaveTextContent('95%');
    expect(twoColumn).toHaveTextContent('73%');

    // The first checked template is shown in detail; another can be selected.
    expect(screen.getByText('Template: Single Column')).toBeInTheDocument();
    fireEvent.click(within(twoColumn).getByRole('button', { name: /View details/ }));
    expect(screen.getByText('Template: Two Column')).toBeInTheDocument();
    expect(within(twoColumn).getByRole('button', { pressed: true })).toBeInTheDocument();
  });

  it('explains why no check is possible without a saved resume', () => {
    renderPanel({ resumeId: null });
    expect(screen.getByText('Save or upload a resume to run a parse check.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Run parse check' })).not.toBeInTheDocument();
  });

  it('is collapsed by default and toggles with the keyboard-accessible header', () => {
    render(<ParseCheckPanel resumeId="resume-1" settings={DEFAULT_TEMPLATE_SETTINGS} />);
    const header = screen.getByRole('button', { name: /ATS Parse Check/ });
    expect(header).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: 'Run parse check' })).not.toBeInTheDocument();
    fireEvent.click(header);
    expect(header).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('button', { name: 'Run parse check' })).toBeInTheDocument();
    expect(
      screen.getByText('Run a check to see how an ATS-style extractor reads this resume.')
    ).toBeInTheDocument();
  });
});
