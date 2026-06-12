"""api.chroma_client — lazy, reclaimable ChromaDB client for the API layer.

ARCHITECTURAL DECISION: lazy load + idle eviction instead of a
process-lifetime client.

The backend held ~1.2GB in RAM indefinitely after the first request,
because a `chromadb.PersistentClient` was created at first use and never
released. On the M720q home server that idle footprint compounds across
every co-hosted app. This module makes ChromaDB a *reclaimable* resource:
it loads on first use (5–10s cold start), serves warm requests instantly,
and a daemon thread evicts it after CHROMA_IDLE_TTL_SECONDS of inactivity,
returning memory to the OS for other tenants.

Why `threading.Lock`, not `asyncio.Lock`: all ChromaDB calls already run
in thread-pool workers via `asyncio.to_thread()`, and the cleanup thread
is not async. An asyncio lock cannot be acquired from that non-async
context; a plain threading lock is the correct primitive for both.

Why not a `threading.Condition`: a Condition would serialise two
concurrent cold starts so only one builds a client. For a single-user
demo app, two requests landing inside the same 5s cold-load window is not
a realistic scenario, so the simpler double-checked plain-lock init is the
right shape — no meaningful downside here.

See docs/rfi_CHROMA_LAZY_LOAD_SPEC.md for the full rationale and the
LEARNING_NOTES companion entry.
"""

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
