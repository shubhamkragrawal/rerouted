"""Internal retrieval records.

These records intentionally remain independent from the API models. The retrieval
index can therefore be rebuilt or replaced without changing the public tool contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class IndexedDocument:
    document_id: str
    text: str
    authority: str
    effective_from: date
    effective_to: date | None = None
    source_url: str = ""
    carrier: str | None = None
    doc_type: str = "regulation"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    jurisdiction: str | None = None

    def active_on(self, as_of_date: date) -> bool:
        """Return whether the document is in force using inclusive endpoints."""

        return self.effective_from <= as_of_date and (
            self.effective_to is None or as_of_date <= self.effective_to
        )
