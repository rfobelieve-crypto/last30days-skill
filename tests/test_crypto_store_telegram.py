"""Tests for the pgvector store and Telegram integration (no DB, no network)."""

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "integrations" / "crypto"))

from pipeline_types import RawItem  # noqa: E402
import pgvector_store  # noqa: E402
from pgvector_store import PgVectorStore, build_rows, schema_ddl, _vector_literal  # noqa: E402
import telegram  # noqa: E402
from telegram import TelegramNotifier, _message_to_raw_item, _split_message  # noqa: E402


def _item(title="t", url="https://x/1", hash_=None, **kw):
    return RawItem(source="l30d:x", source_type="social", title=title, url=url,
                   content_hash=hash_ or "", **kw)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeCursor:
    """Records executes; simulates ON CONFLICT dedup by content_hash (param[7])."""

    def __init__(self, seen_hashes):
        self.seen = seen_hashes
        self.executed = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if params and "INSERT" in sql:
            content_hash = params[7]
            if content_hash in self.seen:
                self.rowcount = 0
            else:
                self.seen.add(content_hash)
                self.rowcount = 1
        else:
            self.rowcount = -1


class FakeConn:
    def __init__(self, seen):
        self.cur = FakeCursor(seen)
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


# --------------------------------------------------------------------------- #
# pgvector store
# --------------------------------------------------------------------------- #


class TestVectorLiteral(unittest.TestCase):
    def test_none(self):
        self.assertIsNone(_vector_literal(None))

    def test_format(self):
        self.assertEqual(_vector_literal([0.1, 0.2, 0.3]), "[0.1,0.2,0.3]")


class TestSchemaAndRows(unittest.TestCase):
    def test_schema_embeds_dim(self):
        self.assertIn("vector(1536)", schema_ddl(1536))
        self.assertIn("content_hash TEXT        NOT NULL UNIQUE", schema_ddl(1024))

    def test_build_rows_serializes_payload_and_vector(self):
        it = _item(content_text="body", published_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
        it.raw_payload = {"score": 9}
        rows = build_rows([it], embeddings=[[0.5] * 4])
        params = rows[0]
        self.assertEqual(params[0], "l30d:x")
        self.assertEqual(json.loads(params[8]), {"score": 9})
        self.assertEqual(params[9], "[0.5,0.5,0.5,0.5]")

    def test_build_rows_no_embeddings(self):
        self.assertIsNone(build_rows([_item()], embeddings=None)[0][9])

    def test_build_rows_misaligned_embeddings_raises(self):
        with self.assertRaises(ValueError):
            build_rows([_item(), _item(url="https://x/2")], embeddings=[[0.1]])


class TestUpsertDedup(unittest.TestCase):
    def setUp(self):
        self.seen = set()
        self.conns = []

        def fake_connect(dsn):
            conn = FakeConn(self.seen)
            self.conns.append(conn)
            return conn

        self.store = PgVectorStore("postgresql://x", embed_dim=4, connect=fake_connect)

    def test_counts_only_new_rows(self):
        items = [_item(url="https://x/1"), _item(url="https://x/2"), _item(url="https://x/3")]
        self.assertEqual(self.store.upsert_items(items), 3)
        # Re-upsert same items -> all duplicates by content_hash.
        self.assertEqual(self.store.upsert_items(items), 0)

    def test_partial_overlap(self):
        self.store.upsert_items([_item(url="https://x/1")])
        new = self.store.upsert_items([_item(url="https://x/1"), _item(url="https://x/2")])
        self.assertEqual(new, 1)

    def test_empty_is_noop(self):
        self.assertEqual(self.store.upsert_items([]), 0)
        self.assertEqual(self.conns, [])

    def test_commit_called(self):
        self.store.upsert_items([_item()])
        self.assertEqual(self.conns[-1].commits, 1)

    def test_embed_fn_invoked_and_validated(self):
        calls = {}

        def embed(texts):
            calls["n"] = len(texts)
            return [[0.1] * 4 for _ in texts]

        store = PgVectorStore("dsn", embed_dim=4, embed_fn=embed,
                              connect=lambda d: FakeConn(set()))
        store.upsert_items([_item(), _item(url="https://x/2")])
        self.assertEqual(calls["n"], 2)

    def test_embed_fn_wrong_dim_raises(self):
        store = PgVectorStore("dsn", embed_dim=4, embed_fn=lambda t: [[0.1] * 3 for _ in t],
                              connect=lambda d: FakeConn(set()))
        with self.assertRaises(ValueError):
            store.upsert_items([_item()])


# --------------------------------------------------------------------------- #
# Telegram notifier
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class TestSplitMessage(unittest.TestCase):
    def test_short_untouched(self):
        self.assertEqual(_split_message("hi", 4096), ["hi"])

    def test_chunks_within_limit(self):
        chunks = _split_message("x" * 9000, 4096)
        self.assertTrue(all(len(c) <= 4096 for c in chunks))
        self.assertEqual(sum(len(c) for c in chunks), 9000)

    def test_prefers_line_boundaries(self):
        text = "\n".join(["line"] * 100)
        for c in _split_message(text, 30):
            self.assertLessEqual(len(c), 30)


class TestNotifier(unittest.TestCase):
    def test_send_posts_to_bot_api(self):
        captured = {}

        def fake_opener(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data.decode()
            return FakeResponse({"ok": True, "result": {"message_id": 1}})

        n = TelegramNotifier("TOKEN123", "-100999", opener=fake_opener)
        resp = n.send("hello *world*")
        self.assertEqual(len(resp), 1)
        self.assertIn("/botTOKEN123/sendMessage", captured["url"])
        self.assertIn("chat_id=-100999", captured["data"])
        self.assertIn("parse_mode=Markdown", captured["data"])

    def test_long_message_multiple_sends(self):
        count = {"n": 0}

        def fake_opener(req, timeout=None):
            count["n"] += 1
            return FakeResponse({"ok": True})

        TelegramNotifier("T", "1", opener=fake_opener).send("y" * 9000)
        self.assertEqual(count["n"], 3)

    def test_failure_does_not_raise(self):
        n = TelegramNotifier("T", "1", opener=lambda r, timeout=None: FakeResponse(
            {"ok": False, "description": "chat not found"}))
        self.assertEqual(n.send("x")[0]["ok"], False)


# --------------------------------------------------------------------------- #
# Telegram channel collector mapping
# --------------------------------------------------------------------------- #


class _FakeMsg:
    def __init__(self, id=1, message="", date=None, views=None):
        self.id = id
        self.message = message
        self.date = date
        self.views = views
        self.forwards = None
        self.replies = None


class TestMessageMapping(unittest.TestCase):
    def test_maps_core_fields(self):
        msg = _FakeMsg(id=42, message="BTC broke $80k\nmore detail",
                       date=datetime(2026, 6, 12, tzinfo=timezone.utc), views=15000)
        item = _message_to_raw_item("WatcherGuru", msg)
        self.assertEqual(item.source, "telegram:WatcherGuru")
        self.assertEqual(item.source_type, "social")
        self.assertEqual(item.title, "BTC broke $80k")
        self.assertEqual(item.url, "https://t.me/WatcherGuru/42")
        self.assertEqual(item.external_id, "42")
        self.assertEqual(item.raw_payload["views"], 15000)

    def test_empty_message_skipped(self):
        self.assertIsNone(_message_to_raw_item("c", _FakeMsg(message="   ")))

    def test_naive_date_gets_utc(self):
        item = _message_to_raw_item("c", _FakeMsg(message="hi", date=datetime(2026, 6, 1)))
        self.assertEqual(item.published_at.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
