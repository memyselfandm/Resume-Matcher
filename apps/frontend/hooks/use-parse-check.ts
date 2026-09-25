'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ParseCheckError } from '@/lib/api/parse-check';

export type ParseCheckPhase = 'idle' | 'running' | 'done' | 'error';

export interface ParseCheckRequestState<T> {
  phase: ParseCheckPhase;
  data: T | null;
  error: ParseCheckError | null;
  /** Whole seconds since the running request started. */
  elapsedSeconds: number;
  /** Seconds left before a retry is allowed after a 429; 0 when not waiting. */
  retryInSeconds: number;
}

/**
 * Run one parse-check request at a time with elapsed-time and 429 cool-down
 * tracking. A new run or unmount aborts the previous request; an aborted
 * request never updates state.
 */
export function useParseCheckRequest<T>() {
  const [state, setState] = useState<ParseCheckRequestState<T>>({
    phase: 'idle',
    data: null,
    error: null,
    elapsedSeconds: 0,
    retryInSeconds: 0,
  });
  const controllerRef = useRef<AbortController | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [retryAt, setRetryAt] = useState<number | null>(null);

  useEffect(() => () => controllerRef.current?.abort(), []);

  // Tick once per second while a request runs or a retry cool-down is active.
  useEffect(() => {
    if (startedAt === null && retryAt === null) return;
    const tick = () => {
      const now = Date.now();
      setState((previous) => ({
        ...previous,
        elapsedSeconds: startedAt === null ? 0 : Math.floor((now - startedAt) / 1000),
        retryInSeconds: retryAt === null ? 0 : Math.max(0, Math.ceil((retryAt - now) / 1000)),
      }));
      if (retryAt !== null && now >= retryAt) setRetryAt(null);
    };
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [startedAt, retryAt]);

  const run = useCallback(async (request: (signal: AbortSignal) => Promise<T>) => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setRetryAt(null);
    setStartedAt(Date.now());
    setState((previous) => ({
      ...previous,
      phase: 'running',
      error: null,
      elapsedSeconds: 0,
      retryInSeconds: 0,
    }));
    try {
      const data = await request(controller.signal);
      if (controller.signal.aborted) return;
      setState((previous) => ({ ...previous, phase: 'done', data, error: null }));
    } catch (caught) {
      if (controller.signal.aborted) return;
      const error =
        caught instanceof ParseCheckError
          ? caught
          : new ParseCheckError(
              'server',
              caught instanceof Error ? caught.message : String(caught)
            );
      if (error.kind === 'busy' && error.retryAfterSeconds) {
        setRetryAt(Date.now() + error.retryAfterSeconds * 1000);
      }
      setState((previous) => ({
        ...previous,
        phase: 'error',
        error,
        retryInSeconds: error.kind === 'busy' ? (error.retryAfterSeconds ?? 0) : 0,
      }));
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setStartedAt(null);
      }
    }
  }, []);

  const cancel = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setStartedAt(null);
    setState((previous) => ({
      ...previous,
      phase: previous.data ? 'done' : 'idle',
      error: null,
    }));
  }, []);

  const reset = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setStartedAt(null);
    setRetryAt(null);
    setState({ phase: 'idle', data: null, error: null, elapsedSeconds: 0, retryInSeconds: 0 });
  }, []);

  return { ...state, run, cancel, reset };
}
