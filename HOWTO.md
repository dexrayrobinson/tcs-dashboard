# Clip KPI pipeline — HOWTO

Stdlib Python 3.9+ only. No new dependencies.

## What it does
`scripts/pipeline.py` runs in this order, every time:
1. **Detect** — hashes the live tracker tabs (Clip Log, Posted - Unmatched, read via the public gviz CSV the dashboard already uses) and every file in `data/sources/`. If nothing changed since `state/pipeline_state.json`, it stops (idempotent). `--force` overrides.
2. **Tracker first (fill-only)** — matches Instagram export entries (`data/sources/*.json`, IG `reels.json` / `posts_N.json` format) to `Posted - Unmatched` rows of `ig_export_account` by the post time encoded in the IG shortcode (±120 s). Exactly one match → fills **blank** `Caption` / `Post date` only. Never overwrites, never guesses: 0 or 2+ matches go to `state/flagged_rows.csv`. Every proposal is in `state/proposed_changes.csv`; every applied cell is logged in `state/pipeline.log`.
3. **Then dashboard** — re-reads the updated tracker and runs `scripts/clip_kpi.py`, writing `reports/clip_kpi.md` (agent-scannable), `reports/clip_kpi.json` (full per-clip data) and `data/kpi.json` (rollups for the dashboard).

## Metrics (`scripts/clip_kpi.py`)
- Resonance Rate = (saves+shares+comments)/views×1000 (per 1,000 views, not %). If Saves is blank, RR is computed without saves and flagged `*` / `resonance_rate_partial_no_saves`.
- Resonance Reach = same numerator / reach×1000 — only if a `Reach` column exists (not yet in tracker → n/a).
- Clip Longevity = total network views / first post views — only if a `First post views` column exists (not yet → n/a).
- Missing inputs are `n/a` (null), never 0.
- Status: `too_early` (inside window: TikTok 7d, IG 14d, Shorts 28d, long-form YT 90d; multi-platform clips use the longest), `non_performing` (fails **every** computable metric, min 2), `ok`, `insufficient_data`, `no_post_date`. Albums: `too_early` until 28d after last clip; `non_performing` only if every judged clip is.
- Fails: RR band Dim (<5), views below page median, longevity <1.1. All thresholds are in `scripts/kpi_config.json`.

## Run locally
```
python3 scripts/clip_kpi.py                       # live tracker → reports
python3 scripts/clip_kpi.py --source Clip_Log.csv --today 2026-11-02
python3 scripts/pipeline.py                       # full pipeline, proposal mode without a token
GOOGLE_ACCESS_TOKEN=$(gcloud auth print-access-token) python3 scripts/pipeline.py   # writes the tracker
```

## Activate the schedule (pending setup)
1. **Workflow scope:** pushing `.github/workflows/*` needs a token with `workflow` scope (`gh auth refresh -s workflow`). Then `git mv ci/clip-kpi-pipeline.yml .github/workflows/`.
2. **Service account:** create a Google Cloud service account with the Sheets API enabled, share the TCS Clip Albums Tracker with its email as **Editor**, add its JSON key as repo secret `GCP_SA_KEY`. Until it exists the workflow runs in proposal mode (reports still refresh, tracker untouched).
3. Optional: drop new Instagram export JSONs into `data/sources/` — the next run picks them up.
4. Run once by hand: Actions → clip-kpi-pipeline → Run workflow (force).
