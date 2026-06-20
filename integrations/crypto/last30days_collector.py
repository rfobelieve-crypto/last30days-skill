"""last30days → crypto-research Collector adapter.

Wraps the last30days research engine as a single pluggable ``Collector`` for the
crypto investment-research pipeline. It runs the engine once per watchlist topic
via ``--emit=json`` and flattens the structured report into ``RawItem`` rows that
feed the system's own P1/P2/P3 Claude filtering layer.

last30days fills four gaps in the crypto source list:
  - social / community signal (Reddit, X, YouTube, HN) with engagement scoring,
    and an X path that uses browser cookies to dodge the API rate limit
  - prediction-market odds (Polymarket) — real-money signal on crypto events
  - developer activity (GitHub) — repo health for protocol due diligence

It deliberately does NOT cover on-chain metrics, structured funding databases,
or macro feeds — keep the existing collectors for those.

Two extraction modes:
  - "ranked"  (default): consume ``ranked_candidates`` — already deduped and
    cross-source merged, each carrying ``final_score`` as a quality prior the
    Claude layer can use for ordering. Fewer, higher-signal items.
  - "raw":    consume ``items_by_source`` — pre-dedup, per-source items if you
    want full control and to run dedup yourself.

Standalone smoke test (offline, uses the engine's mock fixtures)::

    uv run python3 integrations/crypto/last30days_collector.py

Wire into the crypto pipeline by registering an instance in the COLLECTORS table::

    Last30DaysCollector(
        topics=["EigenLayer restaking", "Polymarket crypto ETF", "L2 sequencer"],
        skill_script="skills/last30days/scripts/last30days.py",
    )
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("collector.last30days")

# --------------------------------------------------------------------------- #
# Minimal stand-ins for the crypto system's own types. In the real codebase,
# import RawItem / Collector from the pipeline package and delete these.
# --------------------------------------------------------------------------- #


@dataclass
class RawItem:
    """One normalized signal row, pre-filtering. Mirrors the crypto spec."""

    source: str  # e.g. "l30d:reddit", "l30d:polymarket"
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


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #

# last30days source key -> crypto source_type bucket. Anything unmapped is a
# discussion/social signal, so "social" is the default.
_SOURCE_TYPE_BY_KEY: dict[str, str] = {
    "polymarket": "prediction",
    "github": "dev",
}

# Sources worth pulling for crypto research. Drops short-form video / image
# platforms (tiktok, instagram, pinterest, xiaohongshu) that add noise here.
DEFAULT_CRYPTO_SOURCES: tuple[str, ...] = (
    "reddit",
    "x",
    "hackernews",
    "youtube",
    "polymarket",
    "github",
    "web",
)


def _source_type_for(source_key: str) -> str:
    return _SOURCE_TYPE_BY_KEY.get(source_key, "social")


def _parse_published_at(value: Any) -> datetime | None:
    """Parse last30days date strings ("2026-06-12" or ISO 8601) to aware UTC."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for parse in (
        lambda t: datetime.fromisoformat(t),
        lambda t: datetime.strptime(t, "%Y-%m-%d"),
    ):
        try:
            dt = parse(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    log.debug("unparseable published_at: %r", value)
    return None


class Last30DaysCollector(Collector):
    """Runs the last30days engine per topic and yields RawItems.

    Args:
        topics: research topics / watchlist entries, one engine run each.
        skill_script: path to ``last30days.py``.
        sources: last30days source keys to scope retrieval (``--search``).
        mode: "ranked" (deduped + scored candidates) or "raw" (per-source).
        quick: use the lower-latency retrieval profile (``--quick``).
        timeout: per-topic subprocess timeout in seconds.
        python_bin: interpreter to invoke the engine with (needs Python 3.12+).
        mock: run against the engine's offline fixtures (testing only).
    """

    source = "last30days"
    source_type = "social"  # umbrella; per-item type is refined in fetch()

    def __init__(
        self,
        topics: list[str],
        skill_script: str,
        *,
        sources: tuple[str, ...] = DEFAULT_CRYPTO_SOURCES,
        mode: str = "ranked",
        quick: bool = True,
        timeout: int = 300,
        python_bin: str = sys.executable,
        mock: bool = False,
    ) -> None:
        if mode not in {"ranked", "raw"}:
            raise ValueError(f"mode must be 'ranked' or 'raw', got {mode!r}")
        self.topics = topics
        self.skill_script = skill_script
        self.sources = sources
        self.mode = mode
        self.quick = quick
        self.timeout = timeout
        self.python_bin = python_bin
        self.mock = mock

    # -- public API -------------------------------------------------------- #

    def fetch(self) -> list[RawItem]:
        """Fetch all topics. One failing topic never aborts the others."""
        items: list[RawItem] = []
        for topic in self.topics:
            try:
                report = self._run_engine(topic)
            except subprocess.TimeoutExpired:
                log.warning("last30days timed out for topic %r (%ss)", topic, self.timeout)
                continue
            except subprocess.CalledProcessError as exc:
                log.warning("last30days failed for topic %r: %s", topic, exc.stderr[-500:] if exc.stderr else exc)
                continue
            except json.JSONDecodeError as exc:
                log.warning("last30days emitted invalid JSON for topic %r: %s", topic, exc)
                continue

            for warn in report.get("warnings") or []:
                log.debug("last30days warning [%s]: %s", topic, warn)
            for src, err in (report.get("errors_by_source") or {}).items():
                log.debug("last30days source error [%s/%s]: %s", topic, src, err)

            extracted = self._items_from_report(report, topic)
            log.info("last30days topic %r -> %d items (mode=%s)", topic, len(extracted), self.mode)
            items.extend(extracted)
        return items

    # -- internals --------------------------------------------------------- #

    def _run_engine(self, topic: str) -> dict[str, Any]:
        cmd = [self.python_bin, self.skill_script, topic, "--emit=json"]
        if self.sources:
            cmd += ["--search", ",".join(self.sources)]
        if self.quick:
            cmd.append("--quick")
        if self.mock:
            cmd.append("--mock")
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.timeout, check=True
        )
        return json.loads(proc.stdout)

    def _items_from_report(self, report: dict[str, Any], topic: str) -> list[RawItem]:
        if self.mode == "raw":
            return self._from_items_by_source(report, topic)
        return self._from_ranked_candidates(report, topic)

    def _from_ranked_candidates(self, report: dict[str, Any], topic: str) -> list[RawItem]:
        out: list[RawItem] = []
        for cand in report.get("ranked_candidates") or []:
            url = cand.get("url") or ""
            title = cand.get("title") or ""
            if not (url or title):
                continue
            # Representative underlying item carries the timestamp + full body.
            rep = (cand.get("source_items") or [{}])[0]
            primary_source = cand.get("source") or rep.get("source") or "unknown"
            out.append(
                RawItem(
                    source=f"l30d:{primary_source}",
                    source_type=_source_type_for(primary_source),
                    title=title or rep.get("title", ""),
                    url=url or rep.get("url", ""),
                    content_text=rep.get("body") or cand.get("snippet") or rep.get("snippet", ""),
                    published_at=_parse_published_at(rep.get("published_at")),
                    external_id=cand.get("item_id") or rep.get("item_id"),
                    raw_payload={
                        "topic": topic,
                        "merged_sources": cand.get("sources") or [primary_source],
                        "final_score": cand.get("final_score"),
                        "rerank_score": cand.get("rerank_score"),
                        "engagement": cand.get("engagement") or rep.get("engagement") or {},
                        "explanation": cand.get("explanation"),
                        "cluster_id": cand.get("cluster_id"),
                    },
                )
            )
        return out

    def _from_items_by_source(self, report: dict[str, Any], topic: str) -> list[RawItem]:
        out: list[RawItem] = []
        for source_key, rows in (report.get("items_by_source") or {}).items():
            for it in rows or []:
                url = it.get("url") or ""
                title = it.get("title") or ""
                if not (url or title):
                    continue
                out.append(
                    RawItem(
                        source=f"l30d:{source_key}",
                        source_type=_source_type_for(source_key),
                        title=title,
                        url=url,
                        content_text=it.get("body") or it.get("snippet", ""),
                        published_at=_parse_published_at(it.get("published_at")),
                        external_id=it.get("item_id"),
                        raw_payload={
                            "topic": topic,
                            "engagement": it.get("engagement") or {},
                            "author": it.get("author"),
                            "container": it.get("container"),
                            "relevance_hint": it.get("relevance_hint"),
                        },
                    )
                )
        return out


# --------------------------------------------------------------------------- #
# Offline smoke test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    script = "skills/last30days/scripts/last30days.py"

    print("=== mode=ranked ===")
    ranked = Last30DaysCollector(["test crypto"], script, mock=True, mode="ranked").fetch()
    for item in ranked[:5]:
        print(
            f"[{item.source_type:10}] {item.source:18} score="
            f"{item.raw_payload.get('final_score')!s:8.8} {item.title[:60]}"
        )
    print(f"-> {len(ranked)} ranked items\n")

    print("=== mode=raw (per-source) ===")
    raw = Last30DaysCollector(["test crypto"], script, mock=True, mode="raw").fetch()
    by_type: dict[str, int] = {}
    for item in raw:
        by_type[item.source_type] = by_type.get(item.source_type, 0) + 1
    print(f"-> {len(raw)} raw items by type: {by_type}")
    assert all(i.content_hash for i in ranked + raw), "content_hash must be populated"
    print("\nOK: content_hash populated on all items.")
