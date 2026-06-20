"""Unit tests for the last30days -> crypto-research Collector adapter.

Parsing tests run on synthetic report dicts (no subprocess). One end-to-end
test drives the real engine in --mock mode.
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "integrations" / "crypto"))

from last30days_collector import (  # noqa: E402
    DEFAULT_CRYPTO_SOURCES,
    Last30DaysCollector,
    RawItem,
    _parse_published_at,
    _source_type_for,
)

SKILL_SCRIPT = str(REPO_ROOT / "skills" / "last30days" / "scripts" / "last30days.py")


def _collector(mode="ranked"):
    return Last30DaysCollector(["x"], SKILL_SCRIPT, mode=mode)


# --------------------------------------------------------------------------- #
# source_type routing
# --------------------------------------------------------------------------- #


class TestSourceTypeRouting(unittest.TestCase):
    def test_polymarket_is_prediction(self):
        self.assertEqual(_source_type_for("polymarket"), "prediction")

    def test_github_is_dev(self):
        self.assertEqual(_source_type_for("github"), "dev")

    def test_default_is_social(self):
        for key in ("reddit", "x", "hackernews", "youtube", "unknown"):
            self.assertEqual(_source_type_for(key), "social")


# --------------------------------------------------------------------------- #
# date parsing
# --------------------------------------------------------------------------- #


class TestParsePublishedAt(unittest.TestCase):
    def test_date_only(self):
        dt = _parse_published_at("2026-06-12")
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 6, 12))
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_iso_with_z(self):
        dt = _parse_published_at("2026-06-12T08:30:00Z")
        self.assertEqual(dt, datetime(2026, 6, 12, 8, 30, tzinfo=timezone.utc))

    def test_iso_with_offset_preserved(self):
        dt = _parse_published_at("2026-06-12T08:30:00+02:00")
        self.assertEqual(dt.utcoffset().total_seconds(), 7200)

    def test_naive_gets_utc(self):
        self.assertEqual(_parse_published_at("2026-06-12T08:30:00").tzinfo, timezone.utc)

    def test_garbage_and_empty(self):
        for bad in ("", None, "not a date", 12345):
            self.assertIsNone(_parse_published_at(bad))


# --------------------------------------------------------------------------- #
# RawItem
# --------------------------------------------------------------------------- #


class TestRawItem(unittest.TestCase):
    def test_content_hash_autocomputed(self):
        item = RawItem(source="l30d:x", source_type="social", title="Hi", url="https://x.com/1")
        self.assertEqual(len(item.content_hash), 64)

    def test_hash_stable_across_case_and_whitespace(self):
        a = RawItem(source="s", source_type="social", title=" Hello ", url="HTTPS://X.com/1 ")
        b = RawItem(source="s", source_type="social", title="hello", url="https://x.com/1")
        self.assertEqual(a.content_hash, b.content_hash)

    def test_explicit_hash_respected(self):
        item = RawItem(source="s", source_type="social", title="t", url="u", content_hash="abc")
        self.assertEqual(item.content_hash, "abc")


# --------------------------------------------------------------------------- #
# report parsing (no subprocess)
# --------------------------------------------------------------------------- #

RANKED_REPORT = {
    "ranked_candidates": [
        {
            "item_id": "c1",
            "source": "polymarket",
            "sources": ["polymarket", "x"],
            "title": "Will BTC ETF be approved?",
            "url": "https://polymarket.com/event/btc-etf",
            "snippet": "Odds at 72%",
            "final_score": 91.2,
            "rerank_score": 0.88,
            "engagement": {"volume": 1_200_000},
            "explanation": "high-volume market",
            "cluster_id": "k1",
            "source_items": [
                {
                    "item_id": "si1",
                    "source": "polymarket",
                    "title": "Will BTC ETF be approved?",
                    "url": "https://polymarket.com/event/btc-etf",
                    "body": "Market resolves Dec 31.",
                    "published_at": "2026-06-12",
                    "engagement": {"volume": 1_200_000},
                }
            ],
        },
        {
            "item_id": "c2",
            "source": "github",
            "sources": ["github"],
            "title": "eigenlayer/contracts release v2",
            "url": "https://github.com/eigenlayer/contracts/releases/v2",
            "snippet": "",
            "final_score": 80.0,
            "source_items": [
                {"source": "github", "published_at": "2026-06-10T00:00:00Z", "body": "changelog"}
            ],
        },
        {"title": "", "url": "", "source_items": []},  # skipped: no title/url
    ],
    "items_by_source": {
        "reddit": [
            {
                "item_id": "r1",
                "source": "reddit",
                "title": "Thoughts on restaking risk?",
                "url": "https://reddit.com/r/ethfinance/1",
                "body": "long discussion",
                "published_at": "2026-06-11",
                "engagement": {"upvotes": 340},
                "author": "u/someone",
            },
            {"title": "", "url": ""},  # skipped
        ],
        "polymarket": [
            {
                "item_id": "p1",
                "source": "polymarket",
                "title": "ETH $4k by July?",
                "url": "https://polymarket.com/event/eth-4k",
                "published_at": "2026-06-09",
            }
        ],
    },
    "warnings": ["x: rate limited"],
    "errors_by_source": {"youtube": "auth missing"},
}


class TestRankedParsing(unittest.TestCase):
    def setUp(self):
        self.items = _collector("ranked")._items_from_report(RANKED_REPORT, "crypto")

    def test_skips_empty_candidate(self):
        self.assertEqual(len(self.items), 2)

    def test_source_prefix_and_type(self):
        poly, gh = self.items
        self.assertEqual(poly.source, "l30d:polymarket")
        self.assertEqual(poly.source_type, "prediction")
        self.assertEqual(gh.source, "l30d:github")
        self.assertEqual(gh.source_type, "dev")

    def test_final_score_and_merged_sources_in_payload(self):
        poly = self.items[0]
        self.assertEqual(poly.raw_payload["final_score"], 91.2)
        self.assertEqual(poly.raw_payload["merged_sources"], ["polymarket", "x"])
        self.assertEqual(poly.raw_payload["topic"], "crypto")

    def test_body_and_date_from_representative_item(self):
        poly = self.items[0]
        self.assertEqual(poly.content_text, "Market resolves Dec 31.")
        self.assertEqual(poly.published_at, datetime(2026, 6, 12, tzinfo=timezone.utc))

    def test_external_id_from_candidate(self):
        self.assertEqual(self.items[0].external_id, "c1")


class TestRawParsing(unittest.TestCase):
    def setUp(self):
        self.items = _collector("raw")._items_from_report(RANKED_REPORT, "crypto")

    def test_skips_empty_rows(self):
        # reddit(1 valid) + polymarket(1) = 2
        self.assertEqual(len(self.items), 2)

    def test_types_and_author_preserved(self):
        by_src = {i.source: i for i in self.items}
        self.assertEqual(by_src["l30d:reddit"].source_type, "social")
        self.assertEqual(by_src["l30d:reddit"].raw_payload["author"], "u/someone")
        self.assertEqual(by_src["l30d:polymarket"].source_type, "prediction")


class TestModeValidation(unittest.TestCase):
    def test_bad_mode_rejected(self):
        with self.assertRaises(ValueError):
            Last30DaysCollector(["x"], SKILL_SCRIPT, mode="bogus")

    def test_default_sources_drop_shortform_video(self):
        for noisy in ("tiktok", "instagram", "pinterest", "xiaohongshu"):
            self.assertNotIn(noisy, DEFAULT_CRYPTO_SOURCES)


# --------------------------------------------------------------------------- #
# end-to-end against the real engine (mock fixtures)
# --------------------------------------------------------------------------- #


@unittest.skipUnless(Path(SKILL_SCRIPT).exists(), "engine script not found")
class TestEngineEndToEnd(unittest.TestCase):
    def test_mock_run_yields_hashed_items(self):
        items = Last30DaysCollector(
            ["test crypto"], SKILL_SCRIPT, mock=True, mode="ranked", timeout=180
        ).fetch()
        self.assertGreater(len(items), 0)
        for it in items:
            self.assertTrue(it.source.startswith("l30d:"))
            self.assertEqual(len(it.content_hash), 64)


if __name__ == "__main__":
    unittest.main()
