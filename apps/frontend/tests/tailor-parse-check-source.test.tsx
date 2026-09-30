import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import TailorPage from '@/app/(default)/tailor/page';
import type { ResumeListItem } from '@/lib/api/resume';

const api = vi.hoisted(() => ({
  list: vi.fn(),
  push: vi.fn(),
  back: vi.fn(),
}));
const router = { push: api.push, back: api.back };
const t = (key: string) => key;
vi.mock('next/navigation', () => ({ useRouter: () => router }));
vi.mock('@/lib/i18n', () => ({ useTranslations: () => ({ t, locale: 'es' }) }));
vi.mock('@/lib/api/resume', () => ({
  uploadJobDescriptions: vi.fn(),
  previewImproveResume: vi.fn(),
  confirmImproveResume: vi.fn(),
  fetchResumeList: api.list,
  toPageFitSettings: vi.fn(),
}));
vi.mock('@/lib/api/config', () => ({
  fetchPromptConfig: async () => ({ prompt_options: [], default_prompt_id: 'keywords' }),
}));
vi.mock('@/components/common/resume_previewer_context', () => ({
  useResumePreview: () => ({ setImprovedData: vi.fn() }),
}));
vi.mock('@/lib/context/status-cache', () => ({
  useStatusCache: () => ({
    status: { llm_configured: true },
    isLoading: false,
    incrementJobs: vi.fn(),
    incrementImprovements: vi.fn(),
    incrementResumes: vi.fn(),
  }),
}));
// The panel's own behavior is covered in ats-parse-check-panel.test.tsx; here
// only the resume it is asked to check matters.
vi.mock('@/components/ats-parse-check/parse-check-panel', () => ({
  ParseCheckPanel: ({ resumeId, lang }: { resumeId: string | null; lang?: string | null }) => (
    <div data-testid="parse-check-panel" data-resume-id={resumeId ?? ''} data-lang={lang ?? ''} />
  ),
}));

function master(id: string, isDefault: boolean, title: string): ResumeListItem {
  return {
    resume_id: id,
    filename: null,
    is_master: true,
    is_default_master: isDefault,
    parent_id: null,
    processing_status: 'ready',
    created_at: '',
    updated_at: '',
    title,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
});

async function renderPage() {
  render(<TailorPage />);
  await act(async () => {});
}

function checkedResumeId() {
  return screen.getByTestId('parse-check-panel').getAttribute('data-resume-id');
}

describe('tailor page parse check', () => {
  it('checks the default master before any choice', async () => {
    api.list.mockResolvedValue([master('m1', false, 'DevRel'), master('m2', true, 'SWE')]);
    await renderPage();
    expect(checkedResumeId()).toBe('m2');
    expect(screen.getByTestId('parse-check-panel')).toHaveAttribute('data-lang', 'es');
  });

  it('follows the source master chosen in the picker', async () => {
    api.list.mockResolvedValue([master('m1', true, 'DevRel'), master('m2', false, 'SWE')]);
    await renderPage();
    expect(checkedResumeId()).toBe('m1');

    fireEvent.click(screen.getByRole('button', { name: 'tailor.selectResume' }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: /SWE/ }));
    expect(checkedResumeId()).toBe('m2');
  });
});
