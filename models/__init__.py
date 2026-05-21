"""models — shared data models for the RAG pipeline.

Exports the common paragraph model so downstream code can write
`from models import Paragraph` without reaching into the submodule.
"""

from models.paragraph import Paragraph

__all__ = ["Paragraph"]
