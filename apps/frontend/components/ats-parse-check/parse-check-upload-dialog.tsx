'use client';

import { useId, useRef, useState, type ChangeEvent, type DragEvent } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import {
  parseCheckFile,
  type ParseCheckContentLanguage,
  type ParseCheckReport,
} from '@/lib/api/parse-check';
import { useTranslations } from '@/lib/i18n';
import { formatBytes } from '@/hooks/use-file-upload';
import { useParseCheckRequest } from '@/hooks/use-parse-check';
import { parseCheckErrorMessage } from '@/lib/utils/parse-check-messages';
import { ParseCheckReportView } from './parse-check-report';
import { ParseCheckStatusAlert, SelfConsistencyNote } from './parse-check-shared';

/** The backend's upload limit and accepted types (see `POST /ats/parse-check`). */
export const PARSE_CHECK_MAX_FILE_SIZE = 4 * 1024 * 1024;
const ACCEPTED_EXTENSIONS = ['.pdf', '.docx', '.doc'];
const ACCEPT = [
  ...ACCEPTED_EXTENSIONS,
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/msword',
].join(',');
const CONTENT_LANGUAGES: ParseCheckContentLanguage[] = [
  'en',
  'es',
  'fr',
  'pt',
  'de',
  'ja',
  'ko',
  'zh',
];

type FileProblem = 'invalidType' | 'tooLarge' | null;

function validate(file: File): FileProblem {
  const name = file.name.toLowerCase();
  if (!ACCEPTED_EXTENSIONS.some((extension) => name.endsWith(extension))) return 'invalidType';
  if (file.size > PARSE_CHECK_MAX_FILE_SIZE) return 'tooLarge';
  return null;
}

interface ParseCheckUploadDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** Standalone parse check of any uploaded PDF/DOCX; the file is never stored. */
export function ParseCheckUploadDialog({ open, onOpenChange }: ParseCheckUploadDialogProps) {
  const { t } = useTranslations();
  const inputRef = useRef<HTMLInputElement>(null);
  const inputId = useId();
  const languageId = useId();
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<FileProblem>(null);
  const [language, setLanguage] = useState<ParseCheckContentLanguage | ''>('');
  const [dragging, setDragging] = useState(false);
  const request = useParseCheckRequest<ParseCheckReport>();
  const running = request.phase === 'running';

  const selectFile = (candidate: File | undefined) => {
    if (!candidate) return;
    const found = validate(candidate);
    setProblem(found);
    setFile(found ? null : candidate);
    request.reset();
  };

  const handleChange = (event: ChangeEvent<HTMLInputElement>) => {
    selectFile(event.target.files?.[0]);
    event.target.value = '';
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    if (!running) selectFile(event.dataTransfer.files?.[0]);
  };

  const start = () => {
    if (!file) return;
    void request.run((signal) =>
      parseCheckFile(file, { contentLanguage: language || undefined, signal })
    );
  };

  const handleOpenChange = (next: boolean) => {
    if (!next) request.cancel();
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
        <div className="p-6 space-y-4">
          <DialogHeader>
            <DialogTitle className="font-serif text-2xl font-bold pr-8">
              {t('atsParseCheck.upload.dialogTitle')}
            </DialogTitle>
          </DialogHeader>
          <p className="font-sans text-sm">{t('atsParseCheck.upload.description')}</p>
          <SelfConsistencyNote />

          <div>
            <p
              id={`${inputId}-label`}
              className="block font-mono text-xs font-bold uppercase tracking-wider mb-1"
            >
              {t('atsParseCheck.upload.fileLabel')}
            </p>
            <div
              onDragOver={(event) => {
                event.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={handleDrop}
              className={`flex flex-wrap items-center gap-3 border border-black p-4 ${
                dragging ? 'bg-blue-100' : 'bg-white'
              }`}
            >
              <input
                ref={inputRef}
                id={inputId}
                type="file"
                accept={ACCEPT}
                onChange={handleChange}
                disabled={running}
                tabIndex={-1}
                aria-labelledby={`${inputId}-label`}
                className="sr-only"
              />
              <Button
                variant="outline"
                onClick={() => inputRef.current?.click()}
                disabled={running}
                aria-describedby={`${inputId}-label ${inputId}-hint`}
              >
                {t('atsParseCheck.upload.chooseFile')}
              </Button>
              <span id={`${inputId}-hint`} className="font-sans text-sm text-ink-soft">
                {file
                  ? t('atsParseCheck.upload.selectedFile', {
                      name: file.name,
                      size: formatBytes(file.size, 1),
                    })
                  : t('atsParseCheck.upload.dropHint')}
              </span>
            </div>
            {problem && (
              <p role="alert" className="mt-1 font-sans text-sm text-red-600">
                {t(`atsParseCheck.upload.${problem}`)}
              </p>
            )}
          </div>

          <div>
            <label
              htmlFor={languageId}
              className="block font-mono text-xs font-bold uppercase tracking-wider mb-1"
            >
              {t('atsParseCheck.upload.contentLanguage')}
            </label>
            <select
              id={languageId}
              value={language}
              onChange={(event) =>
                setLanguage(event.target.value as ParseCheckContentLanguage | '')
              }
              disabled={running}
              className="w-full sm:w-auto rounded-none border border-black bg-white px-3 py-2 font-sans text-sm focus:outline-none focus:ring-1 focus:ring-blue-700"
            >
              <option value="">{t('atsParseCheck.upload.autoDetect')}</option>
              {CONTENT_LANGUAGES.map((code) => (
                <option key={code} value={code}>
                  {t(`atsParseCheck.upload.languages.${code}`)}
                </option>
              ))}
            </select>
          </div>

          <div className="flex flex-wrap gap-2">
            <Button onClick={start} disabled={!file || running}>
              {request.data ? t('atsParseCheck.rerun') : t('atsParseCheck.run')}
            </Button>
            {running && (
              <Button variant="outline" onClick={request.cancel}>
                {t('atsParseCheck.cancel')}
              </Button>
            )}
          </div>

          <ParseCheckStatusAlert
            running={running}
            runningMessage={t('atsParseCheck.running')}
            elapsedSeconds={request.elapsedSeconds}
            error={request.error ? parseCheckErrorMessage(t, request.error) : null}
          />

          {!running && request.data && <ParseCheckReportView report={request.data} />}
        </div>
      </DialogContent>
    </Dialog>
  );
}
