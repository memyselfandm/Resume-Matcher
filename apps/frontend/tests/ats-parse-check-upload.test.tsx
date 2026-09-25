import { fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ParseCheckUploadDialog } from '@/components/ats-parse-check/parse-check-upload-dialog';
import { translate } from '@/lib/i18n/server';
import { jsonResponse, makeReport } from './ats-parse-check-fixtures';

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

function fileInput(): HTMLInputElement {
  return document.querySelector('input[type="file"]') as HTMLInputElement;
}

function choose(file: File) {
  fireEvent.change(fileInput(), { target: { files: [file] } });
}

describe('ParseCheckUploadDialog', () => {
  it('uploads the chosen file with the selected language and shows the report', async () => {
    fetchMock.mockResolvedValue(jsonResponse(makeReport()));
    render(<ParseCheckUploadDialog open onOpenChange={() => undefined} />);

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText(/never stored/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Self-consistency check/)).toBeInTheDocument();
    const run = within(dialog).getByRole('button', { name: 'Run parse check' });
    expect(run).toBeDisabled();

    choose(new File(['%PDF-1.7'], 'resume.pdf', { type: 'application/pdf' }));
    expect(within(dialog).getByText(/Selected: resume\.pdf/)).toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText('Resume language'), {
      target: { value: 'de' },
    });
    fireEvent.click(run);

    expect(await within(dialog).findByTestId('parse-check-report')).toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/ats/parse-check');
    const body = init.body as FormData;
    expect((body.get('file') as File).name).toBe('resume.pdf');
    expect(body.get('content_language')).toBe('de');
    expect(within(dialog).getByTestId('score-parseability')).toHaveTextContent('42/100');
  });

  it('omits the language when detection is left automatic', async () => {
    fetchMock.mockResolvedValue(jsonResponse(makeReport()));
    render(<ParseCheckUploadDialog open onOpenChange={() => undefined} />);
    choose(
      new File(['x'], 'cv.docx', {
        type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      })
    );
    fireEvent.click(screen.getByRole('button', { name: 'Run parse check' }));
    await screen.findByTestId('parse-check-report');
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.body as FormData).has('content_language')).toBe(false);
  });

  it('rejects unsupported and oversized files before uploading', () => {
    render(<ParseCheckUploadDialog open onOpenChange={() => undefined} />);
    choose(new File(['x'], 'photo.png', { type: 'image/png' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a PDF, DOCX, or DOC file.');
    expect(screen.getByRole('button', { name: 'Run parse check' })).toBeDisabled();

    choose(new File([new Uint8Array(4 * 1024 * 1024 + 1)], 'big.pdf', { type: 'application/pdf' }));
    expect(screen.getByRole('alert')).toHaveTextContent('The file is larger than 4 MB.');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    [413, 'The file is too large. The limit is 4 MB.'],
    [422, 'The file is not a readable PDF, DOC, or DOCX document.'],
    [504, 'The parse check timed out.'],
  ])('explains HTTP %i from the upload check', async (status, message) => {
    fetchMock.mockResolvedValue(new Response('{"detail":"x"}', { status }));
    render(<ParseCheckUploadDialog open onOpenChange={() => undefined} />);
    choose(new File(['%PDF'], 'resume.pdf', { type: 'application/pdf' }));
    fireEvent.click(screen.getByRole('button', { name: 'Run parse check' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
  });

  it('reports a legacy .doc as an unsupported format', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        makeReport({
          file_format: 'doc',
          extractability: 'unsupported_format',
          overall_score: null,
          content_score: null,
          profiles: [],
          checks: [
            {
              id: 'file_format',
              category: 'extraction',
              severity: 'fatal',
              status: 'fail',
              params: { format: 'doc', supported: ['pdf', 'docx'] },
              evidence: {},
            },
          ],
        })
      )
    );
    render(<ParseCheckUploadDialog open onOpenChange={() => undefined} />);
    choose(new File(['x'], 'old.doc', { type: 'application/msword' }));
    fireEvent.click(screen.getByRole('button', { name: 'Run parse check' }));
    expect(await screen.findByText(/Legacy \.doc files are not analyzed/)).toBeInTheDocument();
    expect(screen.getByText('Unsupported format')).toBeInTheDocument();
    expect(screen.getByTestId('score-parseability')).toHaveTextContent('N/A');
  });
});
