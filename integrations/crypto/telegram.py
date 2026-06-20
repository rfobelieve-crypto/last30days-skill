"""Telegram integration for the crypto-research pipeline — two directions.

1. OUTPUT — `TelegramNotifier`: push alerts / briefings to a Telegram chat via the
   Bot API. Pure stdlib (urllib), no dependencies. Use this to deliver the daily
   digest or high-signal alerts after the P1/P2/P3 filtering layer.

2. INPUT — `TelegramChannelCollector`: pull recent messages from public Telegram
   channels as `social` RawItems. Telegram is a major crypto-alpha venue and the
   last30days engine has no Telegram source, so this is purely additive — register
   it alongside `Last30DaysCollector` in your COLLECTORS table. Requires Telethon
   (MTProto, a user session) because the Bot API cannot read arbitrary channel
   history unless the bot is a channel admin.

--------------------------------------------------------------------------- #
OUTPUT — delivery
--------------------------------------------------------------------------- #

    from telegram import TelegramNotifier
    notifier = TelegramNotifier(bot_token=os.environ["TG_BOT_TOKEN"],
                                chat_id=os.environ["TG_CHAT_ID"])
    notifier.send("*Daily crypto digest*\n- BTC ETF odds 72% (Polymarket)\n...")

Bot setup: talk to @BotFather to create a bot and get the token; add the bot to
your channel/group (or DM it) and read the numeric chat_id from getUpdates.

--------------------------------------------------------------------------- #
INPUT — collection
--------------------------------------------------------------------------- #

    from telegram import TelegramChannelCollector
    collector = TelegramChannelCollector(
        channels=["whale_alert", "WatcherGuru", "DeFi_Alpha"],
        api_id=int(os.environ["TG_API_ID"]),
        api_hash=os.environ["TG_API_HASH"],
        session="crypto_research",   # Telethon session name/file
        lookback_days=30,
    )
    items = collector.fetch()

Auth setup: get `api_id` / `api_hash` from https://my.telegram.org → API
development tools. First run prompts for phone + login code to create the session
file; reuse it after that. `pip install telethon`.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from pipeline_types import Collector, RawItem

log = logging.getLogger("telegram")

_TG_API = "https://api.telegram.org"
_MSG_LIMIT = 4096  # Telegram hard cap per message


# --------------------------------------------------------------------------- #
# OUTPUT: notifier
# --------------------------------------------------------------------------- #


class TelegramNotifier:
    """Send messages to a chat via the Telegram Bot API (stdlib only).

    `opener` is injectable for testing (defaults to urllib.request.urlopen).
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str | int,
        *,
        parse_mode: str | None = "Markdown",
        disable_preview: bool = True,
        timeout: int = 15,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.parse_mode = parse_mode
        self.disable_preview = disable_preview
        self.timeout = timeout
        self._opener = opener

    def send(self, text: str) -> list[dict[str, Any]]:
        """Send text, splitting into <=4096-char chunks. Returns API responses."""
        responses = []
        for chunk in _split_message(text, _MSG_LIMIT):
            responses.append(self._send_one(chunk))
        return responses

    def _send_one(self, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": self.disable_preview,
        }
        if self.parse_mode:
            payload["parse_mode"] = self.parse_mode
        data = urllib.parse.urlencode(payload).encode("utf-8")
        url = f"{_TG_API}/bot{self.bot_token}/sendMessage"
        req = urllib.request.Request(url, data=data, method="POST")
        with self._opener(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok", False):
            log.warning("telegram sendMessage failed: %s", body.get("description"))
        return body


def _split_message(text: str, limit: int) -> list[str]:
    """Split on line boundaries where possible, hard-split overlong lines."""
    if len(text) <= limit:
        return [text]
    chunks, buf = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # single line longer than the cap
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks


# --------------------------------------------------------------------------- #
# INPUT: channel collector
# --------------------------------------------------------------------------- #


def _message_to_raw_item(channel: str, msg: Any) -> RawItem | None:
    """Map a Telethon message object to a RawItem. None for empty messages."""
    text = (getattr(msg, "message", None) or getattr(msg, "text", None) or "").strip()
    if not text:
        return None  # skip media-only / service messages with no text

    msg_id = getattr(msg, "id", None)
    title = text.split("\n", 1)[0][:140]  # first line as a title
    url = f"https://t.me/{channel}/{msg_id}" if msg_id else f"https://t.me/{channel}"

    published_at = getattr(msg, "date", None)
    if isinstance(published_at, datetime) and published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)

    return RawItem(
        source=f"telegram:{channel}",
        source_type="social",
        title=title,
        url=url,
        content_text=text,
        published_at=published_at,
        external_id=str(msg_id) if msg_id is not None else None,
        raw_payload={
            "channel": channel,
            "views": getattr(msg, "views", None),
            "forwards": getattr(msg, "forwards", None),
            "replies": getattr(getattr(msg, "replies", None), "replies", None),
        },
    )


class TelegramChannelCollector(Collector):
    """Pull recent messages from public Telegram channels as social RawItems."""

    source = "telegram"
    source_type = "social"

    def __init__(
        self,
        channels: list[str],
        *,
        api_id: int,
        api_hash: str,
        session: str = "crypto_research",
        lookback_days: int = 30,
        per_channel_limit: int = 200,
    ) -> None:
        self.channels = channels
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = session
        self.lookback_days = lookback_days
        self.per_channel_limit = per_channel_limit

    def fetch(self) -> list[RawItem]:
        try:
            from telethon.sync import TelegramClient  # lazy optional dep
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "TelegramChannelCollector needs Telethon: `pip install telethon`"
            ) from exc

        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        items: list[RawItem] = []
        with TelegramClient(self.session, self.api_id, self.api_hash) as client:
            for channel in self.channels:
                try:
                    items.extend(self._fetch_channel(client, channel, cutoff))
                except Exception as exc:  # one bad channel never aborts the rest
                    log.warning("telegram channel %r failed: %s", channel, exc)
        return items

    def _fetch_channel(self, client: Any, channel: str, cutoff: datetime) -> list[RawItem]:
        out: list[RawItem] = []
        for msg in client.iter_messages(channel, limit=self.per_channel_limit):
            date = getattr(msg, "date", None)
            if isinstance(date, datetime):
                d = date if date.tzinfo else date.replace(tzinfo=timezone.utc)
                if d < cutoff:
                    break  # iter_messages is newest-first; older follow
            item = _message_to_raw_item(channel, msg)
            if item:
                out.append(item)
        log.info("telegram %r -> %d items", channel, len(out))
        return out


# --------------------------------------------------------------------------- #
# Offline demo (no network): message splitting + message mapping.
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    print("split of a 9000-char body ->", [len(c) for c in _split_message("x" * 9000, _MSG_LIMIT)])

    class _FakeMsg:
        id = 42
        message = "BTC just broke $80k\nVolume spiking across majors."
        date = datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)
        views = 15000
        forwards = 320
        replies = None

    item = _message_to_raw_item("WatcherGuru", _FakeMsg())
    print("mapped:", item.source, "|", item.source_type, "|", item.title)
    print("url:", item.url, "| hash:", item.content_hash[:12])
