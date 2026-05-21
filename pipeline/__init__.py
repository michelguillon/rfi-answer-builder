"""pipeline — the RFI Answer Builder pipeline as an importable package.

What lives here:

  - Excel/document loaders   (pipeline.loaders)
  - Shared data models       (pipeline.models — Row, Paragraph)
  - Shared Mistral client    (pipeline.mistral_helpers)
  - Profiling step           (pipeline.profile)
  - Chunk-preview step       (pipeline.review_chunks)
  - Ingestion step           (pipeline.ingest)
  - Retrieval + generation   (pipeline.query)
  - Evaluation framework     (pipeline.evaluate)

ARCHITECTURAL DECISION: the pipeline is a package, not a folder of scripts.

Each module here remains independently runnable as a CLI
(`python -m pipeline.profile data/foo.xlsx`) — the human-facing
behaviour the README documents is preserved. But the same modules
are now importable by the FastAPI layer (`api/services/profiler.py`
does `from pipeline.profile import ...`) instead of being shelled
out via subprocess.

Why not subprocess-shelling? Two reasons. First, streaming progress
from a subprocess back over SSE means parsing the child's stdout —
fragile and tied to log format. Second, every CLI invocation pays
the cold-start cost of importing chromadb + sentence-transformers
(seconds). For a single-user dev UI those costs would dominate
end-to-end latency. Importing the functions lets the FastAPI process
hold the warm imports across requests and yield events directly.

Why no automatic re-exports here? Each module has its own ergonomic
surface (CLI argparse + `if __name__ == '__main__'`); a flat
`from pipeline import profile_excel` re-export would either duplicate
that surface or hide it. Importers reach into modules by name
(`from pipeline.profile import propose_mapping`), which keeps the
public surface honest.
"""
