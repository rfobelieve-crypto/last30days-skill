"""Shared pipeline types for the crypto-research collectors.

These are stand-ins for the crypto system's own ``RawItem`` / ``Collector``.
Keeping them in one module means the collectors, the pgvector store, and the
Telegram integration all agree on one definition.

**Real integration:** replace this module's contents with re-exports from your
pipeline package, e.g. ``from your_pipeline.types import RawItem, Collector``.
Every other file imports from here, so it's a one-file swap.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class RawItem:
    """One normalized signal row, pre-filtering. Mirrors the crypto spec."""

    source: str  # e.g. "l30d:reddit", "l30d:polymarket", "telegram:whale_alert"
    source_type: str  # "social" | "prediction" | "dev"
    title: str
    url: str
    raw_payload: dict[str, Any] = field(default_factory=dict)
    content_text: str = ""
    published_at: datetime | None = None
    external_id: str | None = None
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            basis = f"{(self.url or '').strip().lower()}|{(self.title or '').strip().lower()}"
            self.content_hash = hashlib.sha256(basis.encode("utf-8")).hexdigest()


class Collector:
    """Pluggable collector interface (subset of the crypto system's base)."""

    source: str = ""
    source_type: str = ""
    enabled: bool = True

    def fetch(self) -> list[RawItem]:  # pragma: no cover - interface
        raise NotImplementedError
