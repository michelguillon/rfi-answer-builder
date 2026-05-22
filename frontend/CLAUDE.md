# frontend/ — operating notes

React + Vite + TypeScript + Tailwind + shadcn/ui. Renders the
ingest and answer workflows on top of the FastAPI backend.
Read the root [CLAUDE.md](../CLAUDE.md) first (privacy, branch
discipline, active memory) and [api/CLAUDE.md](../api/CLAUDE.md)
for the contract this UI consumes. This file covers what is
specific to the frontend.

## shadcn first, always

If a shadcn/ui component covers the need, use it. Don't write a
bespoke Button, Card, Dialog, Progress, Badge, Textarea, Input,
or Table — the project ships them in
[src/components/ui/](src/components/ui/) and you import from
`@/components/ui/<name>`. Composing shadcn primitives into a
domain widget (e.g. `<AnswerCard>` that uses Card + Badge +
Textarea + Button) is correct. Building "our own card from
scratch" is not.

If a need genuinely isn't covered (e.g. a step-timeline
visualisation), build it in [src/components/](src/components/)
— not in the page file, and not by forking a shadcn primitive.
The split is: `src/components/ui/` for shadcn primitives,
`src/components/` for project-specific composed components.

## Typed wrappers in `lib/`, no fetch() in pages

Every backend call goes through [src/lib/api.ts](src/lib/api.ts).
Every SSE consumer goes through [src/lib/sse.ts](src/lib/sse.ts).
Pages import the typed functions and the typed event types —
they do not construct `fetch()` calls or read `EventSource` by
hand. When the backend shape changes, `api.ts` is the only file
the types need to follow.

If a page is tempted to call `fetch('/api/foo')` directly, that
is a signal to add a wrapper to api.ts first. Two reasons:
(1) keeps the type surface honest; (2) future test seams (mock
api.ts, render the page) only work if pages don't reach past it.

## SSE: `useSSE`, never raw EventSource

The hook in [src/lib/sse.ts](src/lib/sse.ts) handles both shapes
the backend exposes:

- GET endpoints (profile, process) — vanilla SSE.
- POST endpoints (approve) — POST returning text/event-stream,
  consumed via fetch + ReadableStream.

Both come back as the same `{type, data}` event objects. The
hook returns `{events, status, error, start, reset}`. Pages
either iterate `events` for rendering OR pass an `onEvent`
callback for side-effects. **Do not** spawn a raw `EventSource`
or call `fetch().body.getReader()` in a page — the hook owns
abort, error normalisation, and the parse logic. Pages that
reach past it lose those properties.

## Verbose provenance is mandatory in AnswerCard

Active memory `feedback-show-provenance` and LEARNING_NOTES
entry 12: every answer renders with full retrieval visibility.
The AnswerCard must show, per answer, every source chunk that
fed generation with its source filename + row + score. No
"summary score" or hover-to-reveal "details" collapsing the
sources by default. The retrieval trace is the feature; hiding
it would defeat the purpose of the UI.

Per source chunk the available fields (typed in
`AnswerSource` in api.ts) are: `rank`, `source_file`, `pair_id`,
`section`, `client`, `score`, `score_type`, `question_text`,
`answer_text`. The minimum visible cluster is rank, source_file,
row (extract from pair_id), score. The chunk's question_text +
answer_text should be available on demand (Dialog or expandable
row) for the reviewer who wants to verify what the chunk
actually said.

## Cross-tenant client mentions: visible warning

Active memory `feedback-cross-tenant-leakage` and LEARNING_NOTES
entries 14 + 19: answers can name past clients verbatim, and
that risk surfaces to the human reviewer per answer. The answer
payload's `mentioned_clients` field is the backend's word-
boundary match against every known past-client name in the
corpus. When non-empty, render a visible warning Badge on the
AnswerCard ("mentions: The Guardian, Publicis") BEFORE the
Accept/Edit/Skip buttons. The user must see the flag before
the action.

No "send directly to client" path exists. Every answer flows
through the human review gate (Accept / Edit / Skip) and then
through the export. The export is the only outbound surface.

## Session ID lives in localStorage

Each workflow stores its session_id in localStorage under a
workflow-scoped key (`rfi.ingest.session_id`,
`rfi.answer.session_id`). On workflow completion (or explicit
"start over"), the key is cleared. The session_id is a per-tab
capability token (see api/CLAUDE.md) — never a user identifier,
never persisted server-side beyond the 24h tmp/ TTL.

If a page loads and finds a session_id in localStorage, it may
attempt to resume — POST /api/ingest/upload or GET .../profile
with the existing id. The backend returns 404 if the session
has been cleaned up; pages handle that by clearing localStorage
and starting fresh.

## No auth, by design

Authentication is intentionally out of scope (see SPEC_UI.md
"What is deliberately out of scope"). The frontend never asks
for a username, never stores a token, never talks to an OAuth
provider. Deployment is expected to put SSO + a reverse proxy
in front; the frontend's only obligation is to be reachable
behind whatever the proxy admits.

## What the frontend must NOT do

- Re-implement backend retry/error logic. If a POST fails,
  surface the backend's error message verbatim. Don't wrap it
  in friendly text that hides what actually broke.
- Cache answer events anywhere outside the active session. The
  generated content can include client-identifying text; it
  belongs in tmp/{sid}/ on the backend and React state for the
  active tab, nowhere else.
- Build a "history" view, a "recent sessions" list, or any
  surface that lists prior workflow runs. Sessions are
  ephemeral state; tools that expose them past the 24h TTL
  would create a new privacy surface to defend.
- Ship a bundled tracking SDK, analytics pixel, error-reporter
  that POSTs payloads to a third-party endpoint. The corpus and
  generated answers must not travel anywhere the user hasn't
  authorised.
