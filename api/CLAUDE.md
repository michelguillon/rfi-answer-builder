# api/ — operating notes

FastAPI backend that wraps the [pipeline/](../pipeline/) package
for the RFI web UI. Read the root [CLAUDE.md](../CLAUDE.md) first
(privacy, Docker, Mistral SDK, ChromaDB, code style, branch
discipline). This file covers what is specific to the backend.

## Import pipeline functions, never shell them

Every backend service that needs pipeline behaviour does it by
importing:

```python
# RIGHT
from pipeline.profile import propose_mapping
async for event in propose_mapping(path):
    yield event
```

```python
# WRONG — never do this
proc = await asyncio.create_subprocess_exec("python", "-m", "pipeline.profile", ...)
async for line in proc.stdout:
    parse(line)  # fragile, ties us to stdout format
```

Two reasons. First, streaming SSE events from a subprocess means
parsing the child's stdout — fragile and tied to log format that
the pipeline modules don't owe us. Second, every CLI invocation
pays the cold-start cost of importing chromadb + sentence-transformers
(seconds). For a single-user dev UI those costs would dominate
end-to-end latency. The FastAPI process holds warm imports across
requests; subprocess-shelling throws that away every call.

The pipeline modules' [pipeline/CLAUDE.md](../pipeline/CLAUDE.md)
guarantees module-level side-effect freedom — `import
pipeline.profile` does not trigger argparse or open a ChromaDB
client. Honour that contract from both sides.

## ChromaDB access: `get_chroma_client()`, never construct directly

Never call `chromadb.PersistentClient()` directly in the `api/`
layer. Always use `get_chroma_client()` from
[api/chroma_client.py](chroma_client.py).

The API layer lazy-loads ChromaDB on first request and a daemon
thread evicts it after `CHROMA_IDLE_TTL_SECONDS` of inactivity, so
an idle backend drops from ~1.2GB to ~50MB instead of pinning the
store for the process lifetime. That only works if every caller
shares the one reclaimable client. A direct `PersistentClient(...)`
opens a second, unmanaged handle that is never evicted and defeats
the whole mechanism. The existing `asyncio.to_thread(...)` wrapping
at each call site is unchanged — wrap `get_chroma_client` instead of
the constructor (`await asyncio.to_thread(get_chroma_client)`).

This is an API-layer rule only. The `pipeline/` modules keep
creating their own short-lived `PersistentClient` directly — CLI
runs are one-shot processes, so lazy eviction buys them nothing.
See docs/rfi_CHROMA_LAZY_LOAD_SPEC.md.

## SSE event format

All streaming endpoints use Server-Sent Events with this shape:

```python
@router.get("/api/<workflow>/<action>")
async def endpoint(...):
    async def stream():
        async for event in service.run(...):
            yield f"data: {json.dumps(event)}\n\n"
    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
```

The two headers matter:

- `Cache-Control: no-cache` — keeps a chatty reverse proxy from
  buffering the response into a single chunk at end-of-stream.
- `X-Accel-Buffering: no` — nginx-specific; same purpose.

Event payloads are JSON objects with a `type` field
(`step` / `proposal` / `progress` / `answer` / `done`) plus a
`data` field whose shape depends on the type. The frontend's typed
`useSSE` hook switches on `type`.

## Sessions are filesystem-backed, not in-memory

`api.session.create_session()` returns a UUID and creates
`tmp/{uuid}/`. Subsequent requests pass the UUID and the router
calls `api.session.get_session_dir(uuid)` — which raises 404 if
the directory does not exist.

Why filesystem? See the ARCHITECTURAL DECISION block in
[api/session.py](session.py): single-purpose internal tool, no
need for a database, debuggable with `ls`, isolated by directory
boundary.

**No in-memory state between requests.** A POST that profiles an
upload writes the proposal to `tmp/{id}/profile.json`. The
subsequent POST that approves it reads `profile.json` back from
disk and writes `config.json`. Don't smuggle state across requests
via a process-global dict — the next request might land on a
restarted process, and the dict is gone.

## The session_id is not an auth token

The UUID is a *capability token* for one ephemeral workflow
directory. It is unguessable enough that a stranger cannot poke
random session IDs to find one that exists, but it does NOT
authenticate the user or grant access to corpus data.

Auth is deliberately out of scope (see [rfi_SPEC.md](../docs/rfi_SPEC.md)
"What is deliberately out of scope"). The intended deployment
puts SSO + a reverse proxy in front of FastAPI; session IDs
serve only to disambiguate concurrent users behind that.

Do **not**:

- Embed user identifiers in the session ID
- Use the session ID to authorise corpus operations (anyone
  admitted by the proxy can query the corpus)
- Long-lived sessions in lieu of auth (the 24-hour TTL exists for
  cleanup, not security)

## Cleanup runs on startup AND hourly

`api.session.cleanup_old_sessions()` is called synchronously from
the lifespan context manager in [api/main.py](main.py) at app
startup. Sessions older than 24 hours are removed.

In addition, the lifespan spawns `cleanup_periodically()` as an
asyncio task that re-runs the sweep every hour for the lifetime
of the process. The task is cancelled cleanly on shutdown.

Why both? The startup sweep guarantees a clean slate at every
restart; the hourly task bounds growth on long-running production
deployments where `restart: unless-stopped` means the process
survives for weeks. The original design was startup-only on the
assumption that internal tools restart daily — that assumption
broke once we deployed behind Cloudflare Tunnel on a home server
where weeks-long uptime is the norm. See LEARNING_NOTES entry 27
for the full upgrade rationale.

## Default config for the answer workflow

When the answerer service runs, it uses the production-recommended
configuration from [LEARNING_NOTES entry 13](../docs/rfi_LEARNING_NOTES.md):

- collection: `rfi_separated_cosine`
- retrieval: `semantic`
- rerank: `crossencoder`
- top_k: 3
- pool_size: 20

These are constants in the answerer service (Step 4), not
user-tunable from the UI. The UI's job is "answer this RFI well";
exposing the experiment matrix to non-technical staff would be a
foot-gun.

## What the backend must NOT do

- Modify pipeline module behaviour (organisational changes are
  fine, behavioural changes get their own LEARNING_NOTES entry).
- Bypass human review on answer generation. Cross-tenant content
  leakage (LEARNING_NOTES entry 14) is unsolved; every generated
  answer must reach the human reviewer with provenance + a
  visible warning if it mentions a non-target client name. No
  "send directly to client" endpoint.
- Persist real RFI content outside `tmp/{session_id}/`. The
  ChromaDB store is the only durable home for corpus chunks;
  everything else lives in the session directory and is cleaned
  up by the TTL sweep.
