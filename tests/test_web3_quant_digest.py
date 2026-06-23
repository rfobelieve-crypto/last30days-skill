"""Tests for the web3+quant digest ranking and formatting (no engine, no network)."""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "integrations" / "crypto"))

from pipeline_types import RawItem  # noqa: E402
import web3_quant_digest as dig  # noqa: E402


def _item(title="t", url="https://x/1", score=None, source="l30d:x"):
    payload = {} if score is None else {"final_score": score}
    return RawItem(source=source, source_type="social", title=title, url=url, raw_payload=payload)


class TestRankItems(unittest.TestCase):
    def test_sorts_by_score_desc(self):
        items = [_item("a", score=10), _item("b", "https://x/2", score=50), _item("c", "https://x/3", score=30)]
        ranked = dig.rank_items(items, top_n=10)
        self.assertEqual([i.title for i in ranked], ["b", "c", "a"])

    def test_truncates_to_top_n(self):
        items = [_item(f"t{i}", f"https://x/{i}", score=i) for i in range(20)]
        self.assertEqual(len(dig.rank_items(items, top_n=5)), 5)

    def test_unscored_sink_to_bottom(self):
        items = [_item("noscore", "https://x/1"), _item("scored", "https://x/2", score=1)]
        self.assertEqual(dig.rank_items(items, top_n=10)[0].title, "scored")


class TestFormatTrack(unittest.TestCase):
    def setUp(self):
        self.track = dig.WEB3

    def test_empty_track(self):
        out = dig.format_track(self.track, [])
        self.assertIn("No new signals", out)
        self.assertIn("Web3", out)

    def test_numbered_with_source_label_and_url(self):
        items = [_item("BTC breaks 80k", "https://t.me/x/1", score=9, source="telegram:WatcherGuru")]
        out = dig.format_track(self.track, items)
        self.assertIn("1. [WatcherGuru] BTC breaks 80k", out)
        self.assertIn("https://t.me/x/1", out)

    def test_respects_top_n(self):
        track = dig.Track("T", "x", ("x",), topics=[], top_n=3)
        items = [_item(f"t{i}", f"https://x/{i}", score=i) for i in range(10)]
        out = dig.format_track(track, items)
        self.assertIn("3. ", out)
        self.assertNotIn("4. ", out)

    def test_long_title_truncated(self):
        out = dig.format_track(self.track, [_item("x" * 200, score=1)])
        self.assertIn("...", out)

    def test_newlines_in_title_flattened(self):
        out = dig.format_track(self.track, [_item("line1\nline2", score=1)])
        self.assertNotIn("line1\nline2", out)


class TestBuildDigest(unittest.TestCase):
    def test_combines_sections_with_date_header(self):
        out = dig.build_digest(["section A", "section B"])
        self.assertIn("Daily digest", out)
        self.assertIn("section A", out)
        self.assertIn("section B", out)


class TestTrackConfig(unittest.TestCase):
    def test_quant_targets_subreddits(self):
        self.assertIn("--subreddits", dig.QUANT.extra_args)

    def test_web3_keeps_prediction_and_dev_sources(self):
        self.assertIn("polymarket", dig.WEB3.sources)
        self.assertIn("github", dig.WEB3.sources)

    def test_quant_drops_polymarket(self):
        self.assertNotIn("polymarket", dig.QUANT.sources)

    def test_both_tracks_registered(self):
        self.assertEqual(set(dig.TRACKS), {"web3", "quant"})


if __name__ == "__main__":
    unittest.main()
