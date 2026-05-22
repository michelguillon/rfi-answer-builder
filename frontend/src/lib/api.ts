/**
 * Typed wrappers for every backend endpoint.
 *
 * One module = one source of truth for the request/response shapes.
 * Every page imports from here; nothing constructs fetch() calls by
 * hand. When the backend shape changes, this file is the only place
 * the types need to follow.
 *
 * ARCHITECTURAL DECISION: all URLs are relative paths starting with
 * /api or /healthz.
 *
 * The Vite dev server proxies /api -> backend (see vite.config.ts);
 * in production the same relative path works behind any reverse
 * proxy. Hard-coding http://localhost:8000 here would couple the
 * frontend bundle to a specific deployment URL. Relative paths
 * stay portable.
 */

// ── Types mirroring backend Pydantic shapes ───────────────────────────

export interface SessionCreated {
  session_id: string;
}

export interface IngestUploadResponse {
  session_id: string;
  filename: string;
  detected_rows: number;
}

export interface ProposalColumn {
  letter: string;
  header: string;
  samples: string[];
  heuristic_role: string;
}

export interface ProfileProposal {
  source_file: string;
  sheet: string;
  header_row: number;
  column_roles: Record<string, string>;
  client: string | null;
  date: string | null;
  reasoning: string;
  columns: ProposalColumn[];
}

export interface AnswerUploadResponse {
  session_id: string;
  filename: string;
  question_count: number;
  question_column: string;
  question_column_header: string;
  sheet: string;
  header_row: number;
  questions_preview: string[];
}

export interface AnswerSource {
  rank: number;
  source_file: string;
  pair_id: string;
  section: string | null;
  client: string | null;
  score: number;
  score_type: string;
  question_text: string;
  answer_text: string;
}

export interface AnswerPayload {
  index: number;
  question: string;
  answer: string;
  refused: boolean;
  confidence: number;
  sources: AnswerSource[];
  pair_ids: string[];
  mentioned_clients: string[];
  row?: number;
}

export interface EditResponse {
  modified: number;
  session_id: string;
}

export interface CorpusStats {
  total_pairs: number;
  source_files: number;
  files: string[];
}

// ── SSE event union types ──────────────────────────────────────────────

// Ingest profile stream
export type ProfileEvent =
  | { type: "step"; data: string }
  | { type: "proposal"; data: ProfileProposal }
  | { type: "done" }
  | { type: "error"; data: string; issues?: string[] };

// Ingest approve stream
export type IngestEvent =
  | { type: "collection"; data: string }
  | { type: "progress"; data: { collection: string; batch: number; total: number } }
  | { type: "complete"; data: { collection: string; chunks: number; note?: string } }
  | { type: "done"; data: { total_chunks: number; corpus_size: number } }
  | { type: "error"; data: string };

// Answer process stream
export type AnswerEvent =
  | {
      type: "progress";
      data: { index: number; total: number; question_text: string };
    }
  | { type: "answer"; data: AnswerPayload }
  | { type: "done"; data: { answered: number; refused: number; total: number } }
  | { type: "error"; data: string };

// ── Fetch wrappers ─────────────────────────────────────────────────────

async function handleJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return (await res.json()) as T;
}

export async function createSession(): Promise<SessionCreated> {
  const res = await fetch("/api/sessions", { method: "POST" });
  return handleJson<SessionCreated>(res);
}

export async function getCorpusStats(): Promise<CorpusStats> {
  const res = await fetch("/api/corpus/stats");
  return handleJson<CorpusStats>(res);
}

export async function uploadIngest(
  sessionId: string,
  file: File,
): Promise<IngestUploadResponse> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(
    `/api/ingest/upload?session_id=${encodeURIComponent(sessionId)}`,
    { method: "POST", body: form },
  );
  return handleJson<IngestUploadResponse>(res);
}

export async function uploadAnswer(
  sessionId: string,
  file: File,
): Promise<AnswerUploadResponse> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(
    `/api/answer/upload?session_id=${encodeURIComponent(sessionId)}`,
    { method: "POST", body: form },
  );
  return handleJson<AnswerUploadResponse>(res);
}

export async function postAnswerEdits(
  sessionId: string,
  overrides: Record<number, string>,
  skipped: number[],
): Promise<EditResponse> {
  const res = await fetch("/api/answer/edit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, overrides, skipped }),
  });
  return handleJson<EditResponse>(res);
}

/**
 * Trigger a download of /api/answer/export by setting window.location.
 * The browser handles Content-Disposition and saves with the proper
 * filename automatically — no need to fetch+blob+anchor-click here.
 */
export function downloadExportUrl(sessionId: string): string {
  return `/api/answer/export?session_id=${encodeURIComponent(sessionId)}`;
}
