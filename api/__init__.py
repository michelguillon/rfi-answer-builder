"""api — FastAPI backend wrapping the RFI pipeline.

What lives here:

  - main.py          FastAPI app, lifespan, router mounting
  - session.py       per-user session directory management
  - routers/         HTTP endpoints, one router per workflow

The backend imports pipeline functions directly
(`from pipeline.profile import ...`) rather than shelling out to
the CLI — see api/CLAUDE.md for the rationale.
"""
