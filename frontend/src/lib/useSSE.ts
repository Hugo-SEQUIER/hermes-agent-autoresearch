"use client";

import { useEffect, useRef, useCallback, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "";

export interface SSEEvent {
  id: string;
  sequence: number;
  event_type: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

export function useRunSSE(
  runId: string | null,
  onEvent: (event: SSEEvent) => void
) {
  const lastSeqRef = useRef(0);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!runId) return;

    let cancelled = false;
    let es: EventSource | null = null;

    function connect() {
      const url = `${API_BASE}/api/research/runs/${runId}/events?stream=true&after=${lastSeqRef.current}`;
      es = new EventSource(url);

      es.onopen = () => {
        if (!cancelled) setConnected(true);
      };

      es.onmessage = (msg) => {
        if (cancelled) return;
        try {
          const data = JSON.parse(msg.data);
          if (data.sequence) lastSeqRef.current = data.sequence;
          onEventRef.current(data);
        } catch {
          // skip malformed
        }
      };

      es.onerror = () => {
        if (cancelled) return;
        setConnected(false);
        es?.close();
        // reconnect after 3s
        setTimeout(() => {
          if (!cancelled) connect();
        }, 3000);
      };
    }

    connect();

    return () => {
      cancelled = true;
      es?.close();
      setConnected(false);
    };
  }, [runId]);

  return { connected };
}

export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: unknown[] = []
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);

  const stableFetcher = useCallback(fetcher, deps);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const result = await stableFetcher();
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(err as Error);
      }
    }

    poll();
    const id = setInterval(poll, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [stableFetcher, intervalMs]);

  return { data, error };
}
