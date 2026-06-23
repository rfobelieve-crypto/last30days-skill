# last30days → crypto-research Collector

Adapter that plugs the last30days research engine into the crypto investment-research
pipeline as a single **social / prediction / dev** signal collector. It fills four
gaps in the crypto source list: community signal (Reddit, X, YouTube, HN),
prediction-market odds (Polymarket), and developer activity (GitHub).

It does **not** cover on-chain metrics, structured funding databases, or macro feeds —
keep your existing collectors for those.

## Files

| File | Purpose |
|---|---|
| `pipeline_types.py` | Shared `RawItem` / `Collector` types (single source of truth) |
| `last30days_collector.py` | The `Last30DaysCollector` adapter + standalone smoke test |
| `web3_quant_digest.py` | Two-track (Web3 + Quant) daily digest → Telegram (the lean, no-DB path) |
| `pgvector_store.py` | `PgVectorStore` — Postgres + pgvector persistence with content_hash dedup |
| `telegram.py` | `TelegramNotifier` (push alerts) + `TelegramChannelCollector` (pull channels) |
| `tests/test_last30days_collector.py` | 21 unit + 1 end-to-end test |
| `tests/test_crypto_store_telegram.py` | 21 store + telegram tests (no DB, no network) |

### How they fit together

```
collectors ──► RawItem[] ──► PgVectorStore.upsert_items()  ──► Postgres+pgvector
  Last30DaysCollector          (dedup on content_hash)            (raw_items table)
  TelegramChannelCollector                                              │
                                                            P1/P2/P3 Claude filter
                                                                        │
                                                          TelegramNotifier.send()  ──► Telegram
```

**Swapping in your real pipeline:** every module imports `RawItem` / `Collector`
from `pipeline_types.py`. Replace that one file's contents with re-exports from
your pipeline package and the rest follows.

## Prerequisites

1. **Python 3.12+** to run the engine (the crypto pipeline itself can be on any version;
   point `python_bin` at a 3.12 interpreter).
2. **A reasoning provider key** for the engine's planner/reranker — one of
   `XAI_API_KEY`, `GEMINI_API_KEY`, or `OPENAI_API_KEY`.
3. **Per-source auth** (each missing one just makes that source warn-and-skip):
   - **X**: `FROM_BROWSER=1` (uses local browser cookies — dodges the API rate limit)
     *or* `XAI_API_KEY` (xAI backend) *or* `XQUIK_API_KEY`.
   - **Web**: one of `BRAVE_API_KEY` / `EXA_API_KEY` / `SERPER_API_KEY`.
   - **Reddit / Hacker News / Polymarket / GitHub / YouTube**: work off public endpoints;
     no key required for basic use.

   See `skills/last30days/scripts/lib/env.py` and the repo's `CONFIGURATION.md` for the
   full matrix.

## Usage

### 1. As a pipeline collector (the intended path)

```python
from last30days_collector import Last30DaysCollector

collector = Last30DaysCollector(
    topics=[                                   # one engine run per topic
        "EigenLayer restaking",
        "Polymarket Bitcoin ETF approval",
        "Base L2 sequencer decentralization",
    ],
    skill_script="skills/last30days/scripts/last30days.py",
    mode="ranked",        # "ranked" = deduped+scored candidates (default)
                          # "raw"    = per-source items, you dedup yourself
    quick=True,           # lower-latency retrieval profile
    python_bin="/path/to/python3.12",
)

raw_items = collector.fetch()   # -> list[RawItem]
# hand raw_items to your P1/P2/P3 Claude filtering layer
```

Each `RawItem` carries:
- `source` — `"l30d:reddit"`, `"l30d:polymarket"`, `"l30d:github"`, …
- `source_type` — `"social"` | `"prediction"` (polymarket) | `"dev"` (github)
- `title`, `url`, `content_text`, `published_at`, `external_id`, `content_hash`
- `raw_payload` — keeps `final_score`, `merged_sources`, `engagement`, `explanation`
  so your ranking prompt has a quality prior to work from.

**Integrating for real:** the top of `last30days_collector.py` defines stand-in
`RawItem` / `Collector` classes so the file runs on its own. In the crypto repo,
replace those with `from your_pipeline import RawItem, Collector` and delete the stubs.
Then register an instance in your `COLLECTORS` table.

### 2. Standalone smoke test (offline)

```bash
uv run python3 integrations/crypto/last30days_collector.py   # uses engine mock fixtures
uv run pytest tests/test_last30days_collector.py -q
```

### 3. Recurring monitoring (prototype the daily pipeline)

Before building custom scheduling/storage, the engine's own watchlist stack is an
equivalent daily-monitor you can validate signal-to-noise on first:

```bash
python3 skills/last30days/scripts/watchlist.py add "EigenLayer restaking" --weekly
python3 skills/last30days/scripts/watchlist.py config delivery "https://hooks.slack.com/..."
python3 skills/last30days/scripts/watchlist.py run-all      # cron: runs all + --store (SQLite)
python3 skills/last30days/scripts/briefing.py generate --weekly
```

Once your Postgres+pgvector pipeline is ready, demote last30days back to just a
collector and move scheduling/filtering/storage into your own stack.

## Lean path: Web3 + Quant daily digest (no database)

For the "quickly know what's new" goal, skip the DB entirely. `web3_quant_digest.py`
runs two tuned tracks through the engine, ranks findings, and pushes a Markdown
digest to Telegram.

```bash
# Offline smoke test (engine mock fixtures, prints instead of sending):
python3 web3_quant_digest.py --dry-run --mock

# Live dry run (real engine, prints digest, no Telegram):
python3 web3_quant_digest.py --dry-run

# Real run -> Telegram:
TG_BOT_TOKEN=... TG_CHAT_ID=... python3 web3_quant_digest.py

# One track only:
python3 web3_quant_digest.py --track quant
```

Track tuning (`WEB3` / `QUANT` constants in the file):
- **Web3** keeps `polymarket` (prediction-market odds) + `github` (dev activity).
- **Quant** drops polymarket and targets `--subreddits algotrading,quant,quantfinance`.

Edit the `topics` lists to match what you follow. Cron for 08:00 daily:

```cron
0 8 * * *  cd /path/to/repo && TG_BOT_TOKEN=xxx TG_CHAT_ID=yyy \
           /usr/bin/python3.12 integrations/crypto/web3_quant_digest.py >> /var/log/digest.log 2>&1
```

Graduate to the Postgres+pgvector path below only when you want cross-week dedup,
semantic search over the archive, or trend-over-time analysis.

## Persisting to Postgres + pgvector

```python
from pgvector_store import PgVectorStore

store = PgVectorStore(dsn="postgresql://user@localhost/crypto", embed_dim=1024)
store.init_schema()                       # creates raw_items + indexes (idempotent)
new_count = store.upsert_items(items)     # ON CONFLICT (content_hash) DO NOTHING
```

- Dedup is automatic on `content_hash`; re-running a collector inserts only new rows.
- Embeddings are optional and pluggable. Anthropic has no embedding endpoint, so use
  Voyage (Anthropic-recommended) or any provider — just pass a callable:
  ```python
  store = PgVectorStore(dsn=..., embed_dim=1024,
                        embed_fn=lambda texts: voyage.embed(texts, model="voyage-3").embeddings)
  ```
  Leave `embed_fn` unset to store rows now and backfill embeddings later.
- Needs `pip install "psycopg[binary]"` for real DB use (imported lazily — tests
  and the offline demo need neither psycopg nor a database).

## Telegram (both directions)

last30days has **no** Telegram source, so this is additive.

**Pull crypto channels as a collector** (register alongside `Last30DaysCollector`):
```python
from telegram import TelegramChannelCollector
TelegramChannelCollector(
    channels=["whale_alert", "WatcherGuru", "DeFi_Alpha"],
    api_id=..., api_hash=..., session="crypto_research", lookback_days=30,
).fetch()   # -> social RawItems
```
Needs Telethon (`pip install telethon`) + `api_id`/`api_hash` from my.telegram.org.
First run prompts for phone + login code to create the session file.

**Push alerts / digests out** (stdlib only, no deps):
```python
from telegram import TelegramNotifier
TelegramNotifier(bot_token=..., chat_id=...).send("*Daily digest*\n- ...")
```
Bot token from @BotFather; messages auto-chunk to Telegram's 4096-char limit.

## Modes at a glance

| | `mode="ranked"` (default) | `mode="raw"` |
|---|---|---|
| Source | `ranked_candidates` | `items_by_source` |
| Dedup | done by engine | none — you dedup |
| Volume | fewer, higher-signal | more, per-source |
| Extra signal | `final_score`, merged sources | raw engagement, author |
| Use when | feeding a scoring layer | you want full control |
