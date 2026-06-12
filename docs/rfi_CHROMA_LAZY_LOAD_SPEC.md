# RFI Backend — ChromaDB Lazy Load + Idle Cleanup

**Scope:** `api/` layer only. Pipeline, CLI, Docker Compose, and ChromaDB data are unchanged.
**Motivation:** The backend holds ~1.2GB in memory indefinitely after the first request.
As more apps are added to the M720q, idle memory becomes a compounding problem across
all tenants. This spec makes ChromaDB a lazy, reclaimable resource.

---

## Problem

The current code creates a `chromadb.PersistentClient` at module scope (or at first
import) and holds it for the lifetime of the process. For a demo app that sees occasional
bursts of usage with long idle gaps, that's 1.2GB of RAM pinned regardless of whether
any query is running.

---

## Goal

| Property | Target |
|---|---|
| Idle memory | ~50MB (FastAPI process only, no ChromaDB) |
| Active memory | ~1.2GB (same as today) |
| First-query latency after idle | 5–10s (user is informed) |
| Subsequent query latency | Unchanged |
| Memory reclaim | Automatic after `CHROMA_IDLE_TTL_SECONDS` of inactivity |
| Manual intervention | None required |
| Reusability | Pattern applies to any future heavy in-memory store on the M720q |

---

## What does NOT change

- ChromaDB data, collections, or query logic
- The API contract — all endpoints unchanged
- Docker Compose — no new services, no volume changes
- The persistent `chroma_db/` volume — data survives restarts
- CLI pipeline scripts — they create their own short-lived `PersistentClient` directly,
  as before. Lazy loading is an API-layer concern only.

---

## Memory reclamation — expectation setting

Setting `_chroma_client = None` releases the Python reference. `gc.collect()` clears
Python-level objects promptly. **However:** ChromaDB uses native C++ libraries (DuckDB
for metadata, hnswlib for vector indices) with their own allocators. Those allocators
may hold pages even after Python releases the reference — RSS will drop meaningfully
but not necessarily to zero immediately.

In practice on Linux the OS reclaims pages as other processes need them. Don't assume
full immediate reclamation; measure RSS via `docker stats` before and after a TTL
eviction cycle to establish the actual baseline for your corpus size.

---

## Behaviour specification

### Startup
ChromaDB is not loaded. The process footprint is ~50MB (FastAPI + uvicorn alone).

### Cold request (first request, or first after idle eviction)
`get_chroma_client()` initialises `chromadb.PersistentClient`, loads all collections
into memory, sets `last_used = now`, returns the client.
Expected duration: 5–10 seconds.

### Warm request
`get_chroma_client()` returns the cached client immediately. Updates `last_used = now`.

### Idle eviction (background)
A daemon thread wakes every 60 seconds. If `now - last_used > CHROMA_IDLE_TTL_SECONDS`,
it calls `unload_chroma()`, which sets the client to None, calls `gc.collect()`, and logs.

### TTL configuration
Controlled by `CHROMA_IDLE_TTL_SECONDS` env var. Default: `300` (5 minutes).
Set to `0` to disable eviction entirely — the client loads once on first request and
is never released. The cleanup thread does not start when TTL is 0.

---

## Thread safety

All reads and writes to `_chroma_client` and `_last_used` are protected by a single
`threading.Lock`.

**Why `threading.Lock`, not `asyncio.Lock`:** All ChromaDB calls already run via
`asyncio.to_thread()`, meaning they execute in thread-pool workers. An asyncio lock
cannot be used from a non-async context (the cleanup thread). A plain `threading.Lock`
is the correct primitive.

**Why not a `threading.Condition`:** A `Condition` would prevent two concurrent
cold-start requests from each building a client in the rare race window. For a
single-user demo app, two requests arriving in the exact 5-second cold-load window
simultaneously is not a realistic scenario. The plain lock with double-checked init
is the right shape here — simpler, no meaningful downside for this deployment.

---

## Full implementation: `api/chroma_client.py`

```python
import chromadb
import threading
import time
import gc
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── module state ──────────────────────────────────────────────────────────────
_chroma_client: Optional[chromadb.PersistentClient] = None
_last_used: float = 0.0
_lock = threading.Lock()

CHROMA_PATH = os.environ.get("CHROMA_PATH", "./chroma_db")
CHROMA_IDLE_TTL = int(os.environ.get("CHROMA_IDLE_TTL_SECONDS", "300"))


# ── public interface ──────────────────────────────────────────────────────────

def get_chroma_client() -> chromadb.PersistentClient:
    """Return the shared ChromaDB client, initialising it if necessary.

    All requests share one client. Cold load takes 5–10s on first call
    or after an idle eviction.
    """
    global _chroma_client, _last_used

    # Fast path — warm client
    with _lock:
        if _chroma_client is not None:
            _last_used = time.time()
            return _chroma_client

    # Slow path — cold init, outside the lock so other requests aren't
    # blocked for the full 5–10s duration
    t0 = time.monotonic()
    logger.info("ChromaDB cold load starting")
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    logger.info("ChromaDB loaded (%.1fs)", time.monotonic() - t0)

    with _lock:
        # Another request may have initialised while we waited (unlikely
        # for a single-user app, handled correctly either way)
        if _chroma_client is None:
            _chroma_client = client
        _last_used = time.time()
        return _chroma_client


def unload_chroma() -> None:
    """Release the ChromaDB client and request Python GC.

    Called automatically by the idle cleanup thread. Also available for
    manual use (test teardown, future management endpoint).

    Note: native C++ memory (DuckDB, hnswlib) may take time to fully
    return to the OS. Measure RSS via `docker stats` rather than assuming
    immediate full reclamation.
    """
    global _chroma_client, _last_used
    with _lock:
        if _chroma_client is None:
            return
        _chroma_client = None
        _last_used = 0.0
    gc.collect()
    logger.info("ChromaDB unloaded")


# ── background cleanup ────────────────────────────────────────────────────────

def _evict_if_idle() -> None:
    with _lock:
        if _chroma_client is None:
            return
        if time.time() - _last_used <= CHROMA_IDLE_TTL:
            return
    logger.info("ChromaDB idle TTL exceeded — evicting")
    unload_chroma()


def _cleanup_loop() -> None:
    """Daemon thread: check for idle eviction every 60 seconds."""
    if CHROMA_IDLE_TTL == 0:
        return
    while True:
        time.sleep(60)
        try:
            _evict_if_idle()
        except Exception:
            logger.exception("ChromaDB idle cleanup error (will retry in 60s)")
```

---

## Lifespan changes — `api/main.py`

```python
# Add to imports
import threading
from api.chroma_client import _cleanup_loop, CHROMA_IDLE_TTL

# In the lifespan context manager, after existing startup code:
if CHROMA_IDLE_TTL > 0:
    t = threading.Thread(
        target=_cleanup_loop,
        daemon=True,
        name="chroma-idle-cleanup"
    )
    t.start()
    logger.info("ChromaDB idle cleanup thread started (TTL=%ds)", CHROMA_IDLE_TTL)
else:
    logger.info("ChromaDB idle cleanup disabled (CHROMA_IDLE_TTL_SECONDS=0)")
```

---

## Callers: `api/services/*.py` and `api/routers/sessions.py`

Every direct `chromadb.PersistentClient(...)` call in the `api/` layer is replaced
with `get_chroma_client()`. The `asyncio.to_thread` wrapping at each call site is
unchanged.

```python
# Before
client = chromadb.PersistentClient(path=CHROMA_PATH)

# After
from api.chroma_client import get_chroma_client
client = get_chroma_client()
```

The `pipeline/` modules are **not changed**.

---

## Frontend — loading state

Detect cold start client-side via a timer. If a request is still pending
after 2 seconds, show:

```
Searching knowledge base...
First query may take a few seconds while the system initialises.
```

Apply to both Answer processing and Ingest profiling views.

---

## Logging — four lines that matter

```
INFO  chroma_client: ChromaDB cold load starting
INFO  chroma_client: ChromaDB loaded (7.2s)
INFO  chroma_client: ChromaDB idle TTL exceeded — evicting
INFO  chroma_client: ChromaDB unloaded
```

---

## Files touched

```
api/
  chroma_client.py     ← NEW
  main.py              ← EDIT: start cleanup thread in lifespan
  services/
    profiler.py        ← EDIT: get_chroma_client() replaces direct PersistentClient
    ingester.py        ← EDIT: same
    answerer.py        ← EDIT: same
  routers/
    sessions.py        ← EDIT: corpus stats endpoint uses get_chroma_client()
frontend/src/
  lib/api.ts or sse.ts ← EDIT: 2s cold-start timer + isSlowLoad flag
  pages/Answer.tsx     ← EDIT: show cold-start message when isSlowLoad
  pages/Ingest.tsx     ← EDIT: same
```

`.env.example` addition:
```
# ChromaDB idle eviction (seconds). 0 = load once on first request, never evict.
CHROMA_IDLE_TTL_SECONDS=300
```

`api/CLAUDE.md` addition:
```
Never call chromadb.PersistentClient() directly in the api/ layer.
Always use get_chroma_client() from api/chroma_client.py.
```

---

## Definition of done

- [ ] `api/chroma_client.py` exists with `get_chroma_client()`, `unload_chroma()`,
      `_evict_if_idle()`, `_cleanup_loop()`, and `threading.Lock`
- [ ] All `api/` callers use `get_chroma_client()` — no direct `PersistentClient`
      calls remain in the API layer
- [ ] Cleanup thread starts in `lifespan()` only when `CHROMA_IDLE_TTL > 0`, daemon=True
- [ ] `CHROMA_IDLE_TTL_SECONDS=0` starts no cleanup thread; client loads lazily on
      first request and is never evicted
- [ ] `CHROMA_IDLE_TTL_SECONDS` in `.env.example` with comment
- [ ] Frontend shows cold-start message after 2s of in-flight request
- [ ] `api/CLAUDE.md` updated with no-direct-PersistentClient rule
- [ ] `docker compose up backend` → process starts at ~50MB RSS
- [ ] First query takes 5–10s and logs "ChromaDB loaded (Xs)"
- [ ] Second query is instant
- [ ] After TTL seconds idle, logs "ChromaDB unloaded" and RSS drops
      (measure via `docker stats` — drop will be meaningful, not necessarily complete)

---

## Open questions

- If a second app on the M720q needs vector search, migrate to ChromaDB server mode
  (one Docker service, shared HTTP client) rather than running two embedded instances.
  At that point, idle eviction becomes irrelevant — the server is always on and shared.
