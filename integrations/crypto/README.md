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
| `last30days_collector.py` | The `Last30DaysCollector` adapter + standalone smoke test |
| `test_..._collector.py` (in `tests/`) | 21 unit + 1 end-to-end test |

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

## Modes at a glance

| | `mode="ranked"` (default) | `mode="raw"` |
|---|---|---|
| Source | `ranked_candidates` | `items_by_source` |
| Dedup | done by engine | none — you dedup |
| Volume | fewer, higher-signal | more, per-source |
| Extra signal | `final_score`, merged sources | raw engagement, author |
| Use when | feeding a scoring layer | you want full control |
