"""
mistral_helpers.py — shared Mistral API utilities
=================================================
Imported by analyse.py, ingest.py, query.py. One source of truth for:
  - the Mistral client (built once from MISTRAL_API_KEY)
  - call_with_retry(): wrap EVERY API call so transient 429 / 5xx failures
    retry with exponential backoff instead of crashing the pipeline.

ARCHITECTURAL DECISION: a shared module, not a copy in each script.
The retry logic is load-bearing and easy to get subtly wrong. Three scripts
need it; three copies would drift out of sync. One module = one place to fix
a bug. (Lifted from the call_with_retry pattern proven in mistral_basics.py.)
"""

import os
import time

from mistralai.client import Mistral
from mistralai.client.errors import MistralError


# Which HTTP statuses are worth retrying.
#   429  = rate limit (matters on the free tier)
#   5xx  = transient server errors
# 400 / 401 / 404 are OUR bug — retrying them just wastes time, so they raise.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def get_client() -> Mistral:
    """Build the Mistral client from the MISTRAL_API_KEY environment variable.

    ARCHITECTURAL DECISION: fail loud and early.
    A missing key is a setup error, not a runtime condition to recover from.
    We raise here so the script stops before making a doomed API call.
    """
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "MISTRAL_API_KEY is not set.\n"
            "  Copy .env.example to .env, add your key, and run via\n"
            "  `docker compose run pipeline ...` (Compose loads .env automatically)."
        )
    return Mistral(api_key=api_key)


def call_with_retry(func, *args, max_retries=5, base_delay=1.0, **kwargs):
    """Call a Mistral SDK method, retrying transient failures with backoff.

    Returns whatever `func` returns. Re-raises immediately if the error is
    not retryable (a bug on our side), or once `max_retries` is exhausted.

    Backoff doubles each attempt (base_delay, 2x, 4x, ...). If the server
    sends a `Retry-After` header we honour that instead — it knows its own
    load best.

    KEY INSIGHT: in this pipeline ingest.py embeds many chunks in a loop.
    One transient 429 mid-loop should not lose the whole run — every API
    call goes through here.
    """
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except MistralError as exc:
            attempt += 1
            # Not retryable, or we have tried enough — give up and raise.
            if exc.status_code not in RETRYABLE_STATUS or attempt > max_retries:
                raise

            retry_after = exc.headers.get("retry-after") if exc.headers else None
            if retry_after and str(retry_after).isdigit():
                delay = float(retry_after)                 # server told us
            else:
                delay = base_delay * (2 ** (attempt - 1))  # exponential backoff

            print(f"  HTTP {exc.status_code} on attempt "
                  f"{attempt}/{max_retries} — retrying in {delay:.1f}s")
            time.sleep(delay)
