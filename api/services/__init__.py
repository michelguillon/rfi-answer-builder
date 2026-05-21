"""api.services — async generators that wrap pipeline.* for SSE.

Each module here wraps one pipeline function as an async generator
that yields `{"type": ..., "data": ...}` events. The HTTP routers
serialise those events as text/event-stream.

ARCHITECTURAL DECISION: services do not import each other.

Each service wraps one pipeline module and is consumed by exactly
one router. Keeping them disjoint means a change to the answerer
(Step 4) cannot ripple into the profiler (this Step 2), and vice
versa. Shared utilities, if any, live in pipeline/ where both
services import them.
"""
