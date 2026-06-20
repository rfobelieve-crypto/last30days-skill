"""Postgres + pgvector persistence layer for crypto-research RawItems.

Takes the `RawItem` rows produced by any collector (last30days, Telegram, …) and
upserts them into a `raw_items` table, deduping on `content_hash`. Embeddings are
optional and pluggable: pass an `embed_fn` to populate the `embedding` vector
column for semantic search, or leave it None and backfill later.

The actual DB driver (psycopg 3) is imported lazily so this module — and its
SQL-building logic — can be imported and unit-tested without psycopg or a live
database. Inject a custom `connect` callable for testing.

Quick start::

    from pgvector_store import PgVectorStore
    store = PgVectorStore(dsn="postgresql://user@localhost/crypto", embed_dim=1024)
    store.init_schema()
    inserted = store.upsert_items(collector.fetch())   # returns count of NEW rows

Embeddings (optional). Anthropic has no embedding endpoint, so use Voyage
(Anthropic-recommended) or any provider; just hand over a callable::

    def embed(texts: list[str]) -> list[list[float]]:
        return voyage_client.embed(texts, model="voyage-3").embeddings
    store = PgVectorStore(dsn=..., embed_dim=1024, embed_fn=embed)
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Sequence

from pipeline_types import RawItem

log = logging.getLogger("store.pgvector")

EmbedFn = Callable[[list[str]], Sequence[Sequence[float]]]

# Columns inserted, in order. `embedding` is cast from a pgvector text literal so
# we don't need the pgvector psycopg adapter registered.
_INSERT_COLUMNS = (
    "source",
    "source_type",
    "title",
    "url",
    "content_text",
    "published_at",
    "external_id",
    "content_hash",
    "raw_payload",
    "embedding",
)


def schema_ddl(embed_dim: int) -> str:
    """DDL for the raw_items table + indexes. Idempotent."""
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS raw_items (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT        NOT NULL,
    source_type  TEXT        NOT NULL,
    title        TEXT        NOT NULL,
    url          TEXT,
    content_text TEXT,
    published_at TIMESTAMPTZ,
    external_id  TEXT,
    content_hash TEXT        NOT NULL UNIQUE,
    raw_payload  JSONB       NOT NULL DEFAULT '{{}}',
    embedding    vector({embed_dim}),
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS raw_items_source_type_idx ON raw_items (source_type);
CREATE INDEX IF NOT EXISTS raw_items_published_idx   ON raw_items (published_at DESC);
""".strip()


# ON CONFLICT (content_hash) DO NOTHING gives us idempotent dedup across runs.
# RETURNING lets us count only the rows that were actually new.
_INSERT_SQL = (
    "INSERT INTO raw_items ("
    + ", ".join(_INSERT_COLUMNS)
    + ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector) "
    + "ON CONFLICT (content_hash) DO NOTHING RETURNING id"
)


def _vector_literal(vec: Sequence[float] | None) -> str | None:
    """Format an embedding as a pgvector text literal: '[0.1,0.2,...]'."""
    if vec is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _row_params(item: RawItem, embedding: Sequence[float] | None) -> tuple:
    """Build the positional params for one INSERT row.

    raw_payload is JSON-serialized here so the module has no dependency on the
    driver's dict adapter; the column is JSONB and Postgres casts the text.
    """
    import json

    return (
        item.source,
        item.source_type,
        item.title,
        item.url or None,
        item.content_text or None,
        item.published_at,
        item.external_id,
        item.content_hash,
        json.dumps(item.raw_payload, default=str),
        _vector_literal(embedding),
    )


def build_rows(items: Sequence[RawItem], embeddings: Sequence[Sequence[float]] | None) -> list[tuple]:
    """Pure helper: map items (+ optional aligned embeddings) to INSERT params."""
    if embeddings is not None and len(embeddings) != len(items):
        raise ValueError(f"embeddings ({len(embeddings)}) misaligned with items ({len(items)})")
    rows = []
    for idx, item in enumerate(items):
        emb = embeddings[idx] if embeddings is not None else None
        rows.append(_row_params(item, emb))
    return rows


def _default_connect(dsn: str):
    import psycopg  # lazy: only needed for real DB use

    return psycopg.connect(dsn)


class PgVectorStore:
    """Idempotent RawItem sink backed by Postgres + pgvector."""

    def __init__(
        self,
        dsn: str,
        *,
        embed_dim: int = 1024,
        embed_fn: EmbedFn | None = None,
        connect: Callable[[str], Any] | None = None,
    ) -> None:
        self.dsn = dsn
        self.embed_dim = embed_dim
        self.embed_fn = embed_fn
        self._connect = connect or _default_connect

    # -- schema ------------------------------------------------------------ #

    def init_schema(self) -> None:
        with self._connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(schema_ddl(self.embed_dim))
            conn.commit()
        log.info("raw_items schema ready (embed_dim=%d)", self.embed_dim)

    # -- writes ------------------------------------------------------------ #

    def upsert_items(self, items: Iterable[RawItem]) -> int:
        """Insert items, skipping content_hash duplicates. Returns NEW row count."""
        items = list(items)
        if not items:
            return 0

        embeddings = self._embed(items) if self.embed_fn else None
        rows = build_rows(items, embeddings)

        inserted = 0
        with self._connect(self.dsn) as conn:
            with conn.cursor() as cur:
                for params in rows:
                    cur.execute(_INSERT_SQL, params)
                    # rowcount is 1 when a row was inserted, 0 on conflict.
                    if cur.rowcount and cur.rowcount > 0:
                        inserted += 1
            conn.commit()
        log.info("upsert: %d new / %d seen (%d duplicates)", inserted, len(items), len(items) - inserted)
        return inserted

    # -- embeddings -------------------------------------------------------- #

    def _embed(self, items: Sequence[RawItem]) -> list[list[float]]:
        # Embed title + content; truncate to keep request sizes sane.
        texts = [f"{it.title}\n\n{it.content_text}"[:8000] for it in items]
        vectors = [list(map(float, v)) for v in self.embed_fn(texts)]  # type: ignore[misc]
        for v in vectors:
            if len(v) != self.embed_dim:
                raise ValueError(f"embed_fn returned dim {len(v)}, expected {self.embed_dim}")
        return vectors


# --------------------------------------------------------------------------- #
# Offline demo: prints the SQL + the params it would send, no DB required.
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    from datetime import datetime, timezone

    demo = [
        RawItem(
            source="l30d:polymarket",
            source_type="prediction",
            title="Will a spot BTC ETF be approved?",
            url="https://polymarket.com/event/btc-etf",
            content_text="Odds at 72%.",
            published_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
            external_id="c1",
            raw_payload={"final_score": 91.2, "engagement": {"volume": 1_200_000}},
        )
    ]
    print("SCHEMA DDL:\n", schema_ddl(1024), "\n")
    print("INSERT SQL:\n", _INSERT_SQL, "\n")
    print("ROW PARAMS:")
    for p in build_rows(demo, embeddings=[[0.1] * 1024]):
        print(" ", p[:9], "embedding=<vector len", p[9].count(",") + 1, ">")
