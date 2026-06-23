#!/usr/bin/env python3
"""Web3 + Quant daily digest — the lean "what's new" runner.

Runs two tuned research tracks through the last30days engine, ranks the
findings, formats a compact Markdown digest per track, and pushes it to
Telegram. No database required — this is the fast-awareness path.

Each track tunes its own sources (web3 leans on Polymarket + GitHub; quant
leans on targeted subreddits + GitHub releases). Tracks run independently:
one failing topic or track never aborts the rest.

Run it::

    # Dry run (prints digest, no Telegram, uses live engine):
    python3 web3_quant_digest.py --dry-run

    # Offline smoke test (engine mock fixtures):
    python3 web3_quant_digest.py --dry-run --mock

    # Real run -> Telegram (needs TG_BOT_TOKEN + TG_CHAT_ID):
    python3 web3_quant_digest.py

    # One track only:
    python3 web3_quant_digest.py --track web3

Cron (08:00 daily)::

    0 8 * * *  cd /path/to/repo && TG_BOT_TOKEN=... TG_CHAT_ID=... \
               /usr/bin/python3.12 integrations/crypto/web3_quant_digest.py >> /var/log/digest.log 2>&1

Env:
    TG_BOT_TOKEN, TG_CHAT_ID   Telegram delivery (omit for --dry-run)
    L30D_PYTHON                Python 3.12+ interpreter for the engine (default: this one)
    plus the engine's provider/source keys (see integrations/crypto/README.md)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from last30days_collector import Last30DaysCollector, RawItem  # noqa: E402
from telegram import TelegramNotifier  # noqa: E402

log = logging.getLogger("digest")

SKILL_SCRIPT = str(
    Path(__file__).resolve().parents[2] / "skills" / "last30days" / "scripts" / "last30days.py"
)


@dataclass
class Track:
    name: str
    emoji: str
    sources: tuple[str, ...]
    topics: list[str]
    extra_args: tuple[str, ...] = ()
    top_n: int = 8  # max items shown in the digest per track


# Web3: prediction markets + dev activity matter, so keep polymarket + github.
WEB3 = Track(
    name="Web3",
    emoji="🔗",
    sources=("x", "reddit", "hackernews", "polymarket", "github", "web"),
    topics=[
        "EigenLayer restaking",
        "Ethereum L2 rollups Base Arbitrum",
        "Solana ecosystem",
        "RWA tokenization",
        "stablecoin regulation",
        "Bitcoin ETF flows",
    ],
)

# Quant: target the quant subreddits and lean on GitHub releases / HN / FinTwit.
# Polymarket dropped — not a quant signal. Papers come through web search.
QUANT = Track(
    name="Quant",
    emoji="📊",
    sources=("x", "reddit", "hackernews", "github", "web"),
    topics=[
        "market making strategy crypto",
        "statistical arbitrage crypto",
        "machine learning trading signals",
        "crypto market microstructure",
        "new quant python library release",
    ],
    extra_args=("--subreddits", "algotrading,quant,quantfinance"),
)

TRACKS: dict[str, Track] = {"web3": WEB3, "quant": QUANT}


# --------------------------------------------------------------------------- #
# Ranking + formatting (pure, testable)
# --------------------------------------------------------------------------- #


def rank_items(items: list[RawItem], top_n: int) -> list[RawItem]:
    """Sort by engine final_score (desc); items without a score sink to the bottom."""
    def score(it: RawItem) -> float:
        val = it.raw_payload.get("final_score")
        return float(val) if isinstance(val, (int, float)) else float("-inf")

    return sorted(items, key=score, reverse=True)[:top_n]


def _label(item: RawItem) -> str:
    return item.source.split(":", 1)[-1]  # "l30d:reddit" -> "reddit"


def format_track(track: Track, items: list[RawItem]) -> str:
    """Render one track's ranked items as a Telegram-Markdown section."""
    header = f"{track.emoji} *{track.name}* — {len(items)} signals"
    if not items:
        return f"{header}\n_No new signals._"
    lines = [header, ""]
    for i, it in enumerate(rank_items(items, track.top_n), 1):
        title = (it.title or "untitled").replace("\n", " ").strip()
        if len(title) > 110:
            title = title[:107] + "..."
        url = it.url or ""
        lines.append(f"{i}. [{_label(it)}] {title}")
        if url:
            lines.append(f"   {url}")
    return "\n".join(lines)


def build_digest(sections: list[str]) -> str:
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"🗞 *Daily digest — {stamp}*\n\n" + "\n\n".join(sections)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


def collect_track(track: Track, *, python_bin: str, mock: bool) -> list[RawItem]:
    collector = Last30DaysCollector(
        topics=track.topics,
        skill_script=SKILL_SCRIPT,
        sources=track.sources,
        extra_args=track.extra_args,
        python_bin=python_bin,
        mock=mock,
    )
    items = collector.fetch()
    log.info("track %s -> %d items", track.name, len(items))
    return items


def run(track_names: list[str], *, python_bin: str, mock: bool, dry_run: bool) -> str:
    sections = []
    for name in track_names:
        track = TRACKS[name]
        items = collect_track(track, python_bin=python_bin, mock=mock)
        sections.append(format_track(track, items))

    digest = build_digest(sections)

    if dry_run:
        print(digest)
        return digest

    token, chat_id = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not (token and chat_id):
        raise SystemExit("TG_BOT_TOKEN and TG_CHAT_ID required (or use --dry-run)")
    TelegramNotifier(bot_token=token, chat_id=chat_id).send(digest)
    log.info("digest delivered to Telegram chat %s", chat_id)
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description="Web3 + Quant daily digest")
    parser.add_argument("--track", choices=[*TRACKS, "both"], default="both")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of sending")
    parser.add_argument("--mock", action="store_true", help="Use engine mock fixtures")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    names = list(TRACKS) if args.track == "both" else [args.track]
    python_bin = os.environ.get("L30D_PYTHON", sys.executable)
    run(names, python_bin=python_bin, mock=args.mock, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
