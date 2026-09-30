'use client';

import { useTranslations } from '@/lib/i18n';

/**
 * Explains what the parse check measures and what it does not. `own` checks a
 * rendered resume against its source (self-consistency); `upload` has no
 * source, so only extraction and layout are checked.
 */
export function SelfConsistencyNote({ variant }: { variant: 'own' | 'upload' }) {
  const { t } = useTranslations();
  return (
    <p className="border-2 border-blue-700 bg-blue-100 p-3 font-sans text-xs">
      {t(variant === 'own' ? 'atsParseCheck.selfConsistency' : 'atsParseCheck.uploadNote')}
    </p>
  );
}

interface ParseCheckStatusAlertProps {
  running: boolean;
  runningMessage: string;
  elapsedSeconds: number;
  error: string | null;
}

/** Live progress while a check runs, or the error of the last attempt. */
export function ParseCheckStatusAlert({
  running,
  runningMessage,
  elapsedSeconds,
  error,
}: ParseCheckStatusAlertProps) {
  const { t } = useTranslations();
  return (
    <div aria-live="polite">
      {running ? (
        <div
          role="status"
          className="flex items-center gap-2 border border-black bg-paper-tint p-3"
        >
          <div className="w-3 h-3 bg-blue-700" aria-hidden="true" />
          <span className="font-mono text-xs uppercase tracking-wider">{runningMessage}</span>
          {/* Not announced: a per-second update would flood screen readers. */}
          <span className="font-mono text-xs tabular-nums" aria-hidden="true">
            {t('atsParseCheck.elapsed', { seconds: elapsedSeconds })}
          </span>
        </div>
      ) : error ? (
        <div role="alert" className="border-2 border-red-600 bg-red-100 p-3">
          <p className="font-mono text-xs font-bold uppercase tracking-wider text-red-700">
            {t('atsParseCheck.errors.title')}
          </p>
          <p className="mt-1 font-sans text-sm">{error}</p>
        </div>
      ) : null}
    </div>
  );
}
