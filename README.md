# TCS Clip Albums dashboard

Static GitHub Pages dashboard (`index.html`). Most sections read the Google Sheets tracker live in the browser.

## Instagram API (live) section

The Instagram tab has an **Instagram API** section for exactly two accounts: **@thatcloudstudio** and **@thatreasoningshow**. It reads `data/ig_live.json`, which is written by a manual script. Nothing refreshes it automatically.

### How to refresh the numbers

1. On the machine that holds the long-lived Facebook user token (default path `~/.secrets/ig_owner_long_token`, or set `IG_TOKEN_FILE`), run from the repo root:

   ```
   python3 scripts/pull_ig_live.py
   ```

   Standard library only. It takes about 2 minutes (about 1,200 posts, one insights call each).
2. It rewrites `data/ig_live.json` (read by the dashboard) and `data/ig_live_media.csv` (one row per post, for spreadsheets). Only numbers go into these files. The token is sent in an `Authorization` header and is never printed, logged or written.
3. Commit the two data files and open a PR.

### What it pulls (Graph API v21.0)

- Profile: `followers_count`, `follows_count`, `media_count`, `username`.
- Account insights for the last 28 days (`metric_type=total_value`): `reach`, `views`, `profile_views`, `accounts_engaged`, `total_interactions`, `likes`, `comments`, `saves`, `shares`, and `follows_and_unfollows` (follows / unfollows / net). `impressions` is also requested, but v21 rejects it for IG users. It is recorded as failed and shown blank.
- Every post (paginated): caption (first 80 chars), type, permalink, timestamp, likes, comments, plus lifetime per-post insights `views`, `reach`, `saved`, `shares` and `total_interactions`.

A blank (`null` in JSON, empty in CSV, `—` on the page) means the API returned an error or no value. It is never filled with 0. Errors are kept in `errors` and `media_insight_failures` for each account in the JSON.

To add or remove accounts, edit `ACCOUNTS` at the top of the script. Only the accounts listed there are pulled.

### Instagram API is the source of record (Dex, 2026-11-23)

For @thatcloudstudio and @thatreasoningshow, the whole dashboard uses the API numbers, not only the Instagram API section:

- Overview totals, the Instagram platform card, the platform split, the per-album Instagram views, the Instagram tab KPIs and the clip table all take API values.
- Each tracker Instagram URL is matched to an API post by shortcode. Where they match, the post takes the API views, reach, likes, comments, shares and saves.
- API posts that are missing from the tracker are added.
- If the API has no value for a metric, the tracker value stays. Nothing is filled with 0.
- Tracker figures that the API replaced stay on the page only as secondary context, labelled "Tracker, superseded by API".
- Other Instagram accounts (@thatcloud2, Stratus, Nimbus, Walkity Talk Studio) keep their tracker numbers.

Scoring is unchanged. IG-RR scores and bands still come from the tracker's IG Scores tab. The "too early/low views" gate (under 100 views) still uses tracker views.
