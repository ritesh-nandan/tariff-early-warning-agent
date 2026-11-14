# Tariff Early-Warning Agent

A prototype that warns a small US importer, before a tariff change lands, which of its
products are exposed, how likely and how soon the change is, and what it would do to
landed cost. It reads public Federal Register notices (Section 232, 301 and 122 actions),
turns each into a structured "threat card" with Claude, matches the cards to the
importer's products by HTS code and country, and simulates duty cost today, in the likely
case and in the worst case.

> Advisory only. Not legal advice and not customs-broker advice.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

(on macOS/Linux: `source .venv/bin/activate`)

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and put your key in it. The key is never stored in code.

```
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-fable-5-2
```

`CLAUDE_MODEL` is optional and can be any Claude model ID; nothing in the code depends on
the model. Without a key the pipeline still runs in **offline mode** using a rule-based
notice reader, and every output is labelled as such.

## Run

```bash
python run.py
```

That one command runs all eight steps and prints progress for each:

| Step | What it does | Code |
|---|---|---|
| 1 | Checks each source, collects notices, HTS rates, Kalshi markets into `data/tariffs.db` | `tariff_agent/collector.py` |
| 2 | Reads each notice into a validated threat card (LLM, strict JSON, one retry) | `reader.py`, `llm.py`, `schemas.py` |
| 3 | Loads the importer CSV | `matcher.py` |
| 4 | Matches products to threats; LLM reviews only ambiguous / derivative matches | `matcher.py`, `concordance.py` |
| 5 | Landed cost today / likely / worst, per unit and per year | `duty.py`, `simulator.py` |
| 6 | Weekly alert with a source URL on every item | `alert.py` |
| 7 | Backtest on real 2025-2026 tariff changes | `backtest.py` |
| 8 | Self-contained dashboard | `dashboard.py` |

Outputs:

- `dashboard/index.html` - open in any browser (no server needed)
- `reports/backtest.md` - backtest results
- `reports/weekly_alert.md` - this week's brief
- `reports/writeup.md` - project write-up
- `logs/llm_calls.jsonl` - one line per LLM call (doc id, model, tokens, cache hit)

Options:

```bash
python run.py --importer path/to/your_products.csv
```

```bash
python run.py --offline --skip-collect
```

`--as-of YYYY-MM-DD` replays the analysis as of another date.

### Your own importer file

CSV with these columns (see `data/sample_importer.csv`):

```
sku,description,hts_code,country_of_origin,unit_cost_usd,annual_units
```

Either pass it with `--importer`, or use the upload box in the dashboard. The dashboard
upload runs in the browser with a JavaScript port of the deterministic matcher and duty
math, so it has no LLM review and only knows base duties for HTS codes already in the
database. `--importer` runs the full pipeline.

## Run on GitHub (scheduled, with a public dashboard link)

`.github/workflows/tariff-agent.yml` runs the tests and the pipeline every Monday, on every
push to `main`, and on demand, then publishes the dashboard with GitHub Pages.

1. Create a new **public** repository on GitHub and push this folder to its `main` branch
   (GitHub Desktop: File > Add local repository > Publish repository, untick "Keep this
   code private"). `.env` is git-ignored and must never be committed.
2. In the repository: **Settings > Pages > Build and deployment > Source: GitHub Actions**.
3. **Settings > Secrets and variables > Actions > New repository secret**: name
   `ANTHROPIC_API_KEY`, value your key. Without it the workflow still runs, in offline mode.
4. Optional, same page, **Variables** tab: `CLAUDE_MODEL` (any Claude model ID) and
   `TARIFF_MAX_LLM_DOCS` (cap on notices read by the LLM per run).
5. **Actions > Tariff early-warning run > Run workflow**. When it finishes, the dashboard is
   at `https://<your-username>.github.io/<repo-name>/`.

The database is cached between runs, so only new notices are sent to the LLM. Everything in
a public repository and on its Pages site is visible to anyone: use the sample importer
there, not a real company's product file.

## Tests

```bash
python -m pytest -q
```

30 tests cover the duty math (`tests/test_duty.py`) and the matcher, schema validation and
status/likelihood logic (`tests/test_matcher.py`).

## Data sources

| Source | Use | Fallback |
|---|---|---|
| Federal Register API (`federalregister.gov/api/v1`) | notices and proclamations since 2025-01-20 | `data/seed_actions.json`: 21 real notices with source URLs, loaded only if the API is unreachable |
| USITC HTS REST API (`hts.usitc.gov/reststop`) | base (column 1 general) duty and descriptions | base duty treated as 0% and flagged |
| Kalshi public API | open tariff markets, shown for context only | section left empty |

Each source is probed at the start of every run and the result is shown in the dashboard.

## Cost and caching

LLM results are cached in SQLite by a hash of task, prompt version, model and prompt
content, so rerunning on unchanged notices costs nothing. A first full run reads about
130 notices (roughly 300k input tokens in total). Notices longer than 80,000 characters
are cut at that length; this is logged and recorded in `documents.truncated`.
`TARIFF_MAX_LLM_DOCS` caps how many notices go to the LLM in one run.

For Claude Fable 5.x, Opus 5.5 and Sonnet 5.5 the wrapper turns on Anthropic's
server-side refusal fallback (a beta feature) so a declined request can be answered by
another Claude model; it switches itself off if the API rejects it.

## Key design decisions

- **Facts vs. judgement.** The LLM extracts what a notice says and judges ambiguous
  matches. Dates, status over time, stacking and all dollar figures are plain Python.
- **Nothing invented.** The reader prompt and schema require null for anything the notice
  does not state. When a pending notice gives no rate, the simulator uses a scenario range
  from `config.ASSUMED_RATE_RANGE_PCT` and labels it "assumed" everywhere.
- **Supersession.** Per legal authority and topic, the latest substantive notice is the
  current state; an in-force duty is kept alongside a newer pending notice on the same topic.
- **Likelihood ages.** A notice's own 90-day estimate is used for 45 days; after that a
  hazard table keyed to days since initiation takes over (`scoring.py`).
- **Stacking.** Highest duty within one authority; different authorities add; Section 122
  does not sit on top of Section 232.

## Known limitations

See `reports/writeup.md`. The most important: offline-mode cards are rough; HTS scope for
investigations comes from a hand-written concordance; exemptions and annex-level carve-outs
are not checked; Section 232 derivative duties are applied to full value rather than metal
content; the likelihood table is tuned on the same events used in the backtest.
