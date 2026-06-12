/**
 * useSSE — typed consumer for Server-Sent Events from FastAPI.
 *
 * The backend has two SSE shapes:
 *   - GET endpoints (profile, process) — vanilla SSE, EventSource works.
 *   - POST endpoints (approve)         — POST returning text/event-stream;
 *                                        EventSource only does GET, so
 *                                        we fall back to fetch + a
 *                                        ReadableStream reader.
 *
 * Both shapes yield the same `data: <json>\n\n` wire format. This hook
 * abstracts the transport away — callers pass {method, url, body} and
 * receive parsed event objects.
 *
 * ARCHITECTURAL DECISION: fetch+ReadableStream for POST, not a polyfill
 * that wraps both behind a fake EventSource interface.
 *
 * Several npm packages claim to "make EventSource support POST" by
 * polyfilling. They either re-implement reconnection logic (fragile),
 * change the wire format, or ship 30 kB of code for what is a 40-line
 * stream parser. The native fetch API gives us a ReadableStream over
 * the response body; a tiny TextDecoder + split-on-double-newline
 * loop yields the same events. Honest, debuggable, zero deps.
 *
 * The hook returns {events, done, error, status}: events accumulate
 * as they arrive (React state, re-renders on each), done flips to
 * true on the {type:"done"} event OR on natural stream close,
 * error captures any thrown error. Pages can either snapshot the
 * full events list or attach a per-event side-effect via onEvent.
 */

import { useEffect, useRef, useState } from "react";

export type SSEMethod = "GET" | "POST";

export interface SSEStartOptions<E> {
  url: string;
  method?: SSEMethod;
  body?: unknown; // JSON-serialised if present and method=POST
  onEvent?: (event: E) => void;
  onDone?: () => void;
  onError?: (err: Error) => void;
}

export interface SSEState<E> {
  events: E[];
  status: "idle" | "open" | "done" | "error";
  error: string | null;
  /**
   * True when a started stream has been open for >2s without delivering
   * its first event. The backend lazy-loads ChromaDB (5–10s cold start
   * after idle, see api/chroma_client.py); during that wait the stream
   * is open but silent. Pages surface a "system is initialising" hint
   * while this is true. Cleared the moment the first event arrives.
   */
  isSlowLoad: boolean;
  start: (opts: SSEStartOptions<E>) => void;
  reset: () => void;
}

// Parse a buffer of accumulated stream text into discrete SSE events.
// Returns [parsedEvents, remainder]. Each event is the JSON object
// inside `data: <json>\n\n`.
function parseSSEBuffer<E>(buffer: string): { events: E[]; remainder: string } {
  const parts = buffer.split("\n\n");
  // Last chunk may be a partial frame — keep it as the remainder.
  const remainder = parts.pop() ?? "";
  const events: E[] = [];
  for (const part of parts) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    // SSE allows multiple "data: " lines per event, joined with \n.
    // Our backend always emits one. Strip the prefix on any line.
    const payload = trimmed
      .split("\n")
      .map((line) => (line.startsWith("data:") ? line.slice(5).trim() : line))
      .join("");
    if (!payload) continue;
    try {
      events.push(JSON.parse(payload) as E);
    } catch (err) {
      // Backend should never emit non-JSON in a data line; log and skip
      // rather than tearing down the whole stream over a parse error.
      console.warn("SSE parse error", { payload, err });
    }
  }
  return { events, remainder };
}

// Cold-start hint fires after this many ms of an open-but-silent stream.
const SLOW_LOAD_MS = 2000;

export function useSSE<E extends { type: string }>(): SSEState<E> {
  const [events, setEvents] = useState<E[]>([]);
  const [status, setStatus] = useState<SSEState<E>["status"]>("idle");
  const [error, setError] = useState<string | null>(null);
  const [isSlowLoad, setIsSlowLoad] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const slowTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Cancel the pending cold-start timer and drop the hint. Called on the
  // first event, on done/error, and on reset/unmount — anything that means
  // the stream is no longer silently waiting.
  const clearSlowLoad = () => {
    if (slowTimerRef.current !== null) {
      clearTimeout(slowTimerRef.current);
      slowTimerRef.current = null;
    }
    setIsSlowLoad(false);
  };

  useEffect(
    () => () => {
      abortRef.current?.abort();
      if (slowTimerRef.current !== null) clearTimeout(slowTimerRef.current);
    },
    [],
  );

  const reset = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    clearSlowLoad();
    setEvents([]);
    setStatus("idle");
    setError(null);
  };

  const start = (opts: SSEStartOptions<E>) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setEvents([]);
    setStatus("open");
    setError(null);

    // Start the cold-start clock. With a warm backend the first event
    // lands in well under 2s and clearSlowLoad cancels this; with a cold
    // ChromaDB the stream stays silent through the 5–10s load and the
    // hint shows. We key off first-event, not fetch resolution, because
    // Starlette flushes SSE headers before the generator body runs — the
    // cold load happens *after* `await fetch` resolves.
    setIsSlowLoad(false);
    slowTimerRef.current = setTimeout(() => {
      slowTimerRef.current = null;
      setIsSlowLoad(true);
    }, SLOW_LOAD_MS);

    const handleEvent = (ev: E) => {
      clearSlowLoad();
      setEvents((prev) => [...prev, ev]);
      opts.onEvent?.(ev);
      if (ev.type === "done") {
        setStatus("done");
        opts.onDone?.();
      } else if (ev.type === "error") {
        const msg = (ev as unknown as { data?: string }).data ?? "stream error";
        setError(msg);
        setStatus("error");
        opts.onError?.(new Error(msg));
      }
    };

    const fail = (err: unknown) => {
      clearSlowLoad();
      if (controller.signal.aborted) return;
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
      setStatus("error");
      opts.onError?.(new Error(msg));
    };

    void (async () => {
      try {
        const init: RequestInit = {
          method: opts.method ?? "GET",
          signal: controller.signal,
          headers:
            opts.method === "POST"
              ? { "Content-Type": "application/json" }
              : undefined,
          body:
            opts.method === "POST" && opts.body !== undefined
              ? JSON.stringify(opts.body)
              : undefined,
        };
        const res = await fetch(opts.url, init);
        if (!res.ok || !res.body) {
          const text = await res.text().catch(() => "");
          throw new Error(`${res.status} ${res.statusText}: ${text}`);
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { value, done: streamDone } = await reader.read();
          if (streamDone) break;
          buffer += decoder.decode(value, { stream: true });
          const { events: parsed, remainder } = parseSSEBuffer<E>(buffer);
          buffer = remainder;
          for (const ev of parsed) handleEvent(ev);
        }
        // Drain any trailing frame.
        if (buffer.trim()) {
          const { events: parsed } = parseSSEBuffer<E>(buffer + "\n\n");
          for (const ev of parsed) handleEvent(ev);
        }
        // If the server closed the stream without an explicit done event,
        // we still consider the run finished — pages can distinguish
        // "stream ended" from "got done event" via the events array.
        clearSlowLoad();
        setStatus((s) => (s === "open" ? "done" : s));
      } catch (err) {
        fail(err);
      }
    })();
  };

  return { events, status, error, isSlowLoad, start, reset };
}
