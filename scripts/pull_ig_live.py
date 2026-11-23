#!/usr/bin/env python3
"""Manual pull of live Instagram Graph API numbers for the TCS dashboard.

Writes numbers only (no token) to:
  data/ig_live.json        - read by index.html (Instagram tab, "Instagram API (live)" section)
  data/ig_live_media.csv   - one row per post, same numbers as the JSON, for spreadsheets

Run by hand (no schedule):
  python3 scripts/pull_ig_live.py
The token is read from IG_TOKEN_FILE (default ~/.secrets/ig_owner_long_token) and sent
only in an Authorization header. It is never printed, logged or written to disk.

Only the accounts in ACCOUNTS below are pulled. Blank values mean the API returned an
error or no value for that metric; they are never filled with 0.
"""
import csv, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API = "https://graph.facebook.com/v21.0"
ACCOUNTS = {  # username -> Instagram business account id. Add accounts here deliberately.
    "thatcloudstudio": "17841445170265029",
    "thatreasoningshow": "17841429023533310",
}
ACCOUNT_METRICS = ["reach", "views", "impressions", "profile_views", "accounts_engaged",
                   "total_interactions", "likes", "comments", "saves", "shares",
                   "follows_and_unfollows"]
MEDIA_METRICS = ["views", "reach", "saved", "shares", "total_interactions", "plays"]
DAYS = 28
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = ZoneInfo("America/New_York")

TOKEN = open(os.path.expanduser(os.environ.get("IG_TOKEN_FILE", "~/.secrets/ig_owner_long_token"))).read().strip()


def get(path_or_url, params=None, tries=3):
    """GET a Graph path. Returns (json, None) or (None, short error string without token)."""
    url = path_or_url if path_or_url.startswith("http") else f"{API}/{path_or_url}"
    u = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k != "access_token"]
    q += list((params or {}).items())
    url = urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), ""))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r), None
        except urllib.error.HTTPError as e:
            try:
                err = json.load(e).get("error", {})
            except Exception:
                err = {}
            msg = f"HTTP {e.code} code {err.get('code')}/{err.get('error_subcode')}: {err.get('message', '')}"[:300]
            if err.get("code") in (1, 2, 4, 17, 32, 613) and i < tries - 1:
                time.sleep(5 * (i + 1)); continue
            return None, msg.replace(TOKEN, "***")
        except Exception as e:
            if i < tries - 1:
                time.sleep(3); continue
            return None, type(e).__name__
    return None, "failed"


def value_of(item):
    if "total_value" in item:
        tv = item["total_value"]
        if "breakdowns" in tv:
            out = {}
            for b in tv["breakdowns"]:
                for r in b.get("results", []):
                    out["_".join(r.get("dimension_values", []))] = r.get("value")
            return {"total": tv.get("value"), **out} if out else tv.get("value")
        return tv.get("value")
    vals = item.get("values") or []
    if len(vals) == 1:
        return vals[0].get("value")
    if vals and all(isinstance(v.get("value"), (int, float)) for v in vals):
        return sum(v["value"] for v in vals)
    return None


def account_insights(ig, since, until):
    out, failed = {}, {}
    for m in ACCOUNT_METRICS:
        p = {"metric": m, "period": "day", "since": since, "until": until, "metric_type": "total_value"}
        if m == "follows_and_unfollows":
            p["breakdown"] = "follow_type"
        d, err = get(f"{ig}/insights", p)
        if err:  # some metrics only accept the time-series form
            p2 = {k: v for k, v in p.items() if k not in ("metric_type", "breakdown")}
            d2, err2 = get(f"{ig}/insights", p2)
            if not err2:
                d, err = d2, None
        if err:
            failed[m] = err; continue
        items = d.get("data") or []
        v = value_of(items[0]) if items else None
        if m == "follows_and_unfollows" and isinstance(v, dict):
            # follow_type breakdown: FOLLOWER = new follows, NON_FOLLOWER = unfollows (Meta's naming)
            fo, un = v.get("FOLLOWER"), v.get("NON_FOLLOWER")
            v = {"follows": fo, "unfollows": un, "net": (fo - un) if fo is not None and un is not None else None}
        if v is None:
            failed[m] = "no value returned"
        else:
            out[m] = v
    return out, failed


def media_insights(m):
    kind = m.get("media_product_type")
    metrics = [x for x in MEDIA_METRICS if not (x == "plays")]  # plays is deprecated in v21+; tried only on failure
    d, err = get(f"{m['id']}/insights", {"metric": ",".join(metrics)})
    vals, failed = {}, {}
    if not err:
        for it in d.get("data", []):
            vals[it["name"]] = value_of(it)
    else:  # try one by one so one bad metric doesn't blank the rest
        for x in metrics:
            d1, e1 = get(f"{m['id']}/insights", {"metric": x})
            if e1:
                failed[x] = e1
            else:
                it = (d1.get("data") or [{}])[0]
                vals[x] = value_of(it) if it else None
    return vals, failed


def main():
    now = datetime.now(timezone.utc)
    until = int(now.timestamp()); since = int((now - timedelta(days=DAYS)).timestamp())
    pulled_et = now.astimezone(ET)
    out = {"source": f"Instagram API (live, pulled {pulled_et:%Y-%m-%d %H:%M} ET)",
           "pulled_at": now.isoformat(timespec="seconds"), "pulled_et": f"{pulled_et:%Y-%m-%d %H:%M}",
           "window_days": DAYS,
           "window": {"since": datetime.fromtimestamp(since, ET).strftime("%Y-%m-%d"),
                      "until": datetime.fromtimestamp(until, ET).strftime("%Y-%m-%d")},
           "api": "graph.facebook.com v21.0", "accounts": []}
    rows = []
    for user, ig in ACCOUNTS.items():
        print(f"[{user}] profile", file=sys.stderr)
        prof, perr = get(ig, {"fields": "username,name,followers_count,follows_count,media_count"})
        acct = {"ig_id": ig, "username": (prof or {}).get("username", user),
                "profile": {k: (prof or {}).get(k) for k in ("name", "followers_count", "follows_count", "media_count")},
                "errors": {}}
        if perr:
            acct["errors"]["profile"] = perr
        ins, ifail = account_insights(ig, since, until)
        acct["insights_28d"] = ins; acct["errors"]["insights_28d"] = ifail
        media, url = [], f"{ig}/media"
        params = {"fields": "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count", "limit": 100}
        while url:
            d, err = get(url, params)
            if err:
                acct["errors"]["media_list"] = err; break
            media += d.get("data", [])
            url = (d.get("paging") or {}).get("next"); params = None
        print(f"[{user}] {len(media)} media, fetching per-post insights", file=sys.stderr)
        with ThreadPoolExecutor(6) as ex:
            res = list(ex.map(media_insights, media))
        fail_count, fail_example = {}, {}
        posts = []
        for m, (vals, failed) in zip(media, res):
            for k, e in failed.items():
                fail_count[k] = fail_count.get(k, 0) + 1; fail_example.setdefault(k, e)
            ts = m.get("timestamp")
            date_et = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(ET).strftime("%Y-%m-%d") if ts else ""
            p = {"id": m["id"], "date": date_et, "timestamp": ts,
                 "caption": " ".join((m.get("caption") or "").split())[:80],
                 "media_type": m.get("media_type"), "media_product_type": m.get("media_product_type"),
                 "permalink": m.get("permalink"), "like_count": m.get("like_count"),
                 "comments_count": m.get("comments_count")}
            for x in ("views", "reach", "saved", "shares", "total_interactions"):
                p[x] = vals.get(x)
            posts.append(p)
            rows.append({"account": acct["username"], **p})
        acct["media_insight_failures"] = {k: {"posts": n, "example": fail_example[k]} for k, n in fail_count.items()}
        acct["media_pulled"] = len(posts)
        acct["posts"] = posts
        out["accounts"].append(acct)
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    js = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    assert TOKEN not in js
    with open(os.path.join(ROOT, "data", "ig_live.json"), "w") as f:
        f.write(js)
    cols = ["account", "id", "date", "timestamp", "caption", "media_type", "media_product_type", "permalink",
            "like_count", "comments_count", "views", "reach", "saved", "shares", "total_interactions"]
    with open(os.path.join(ROOT, "data", "ig_live_media.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    for a in out["accounts"]:
        print(f"{a['username']}: followers={a['profile'].get('followers_count')} posts={a['profile'].get('media_count')} "
              f"pulled={a['media_pulled']} insights={a['insights_28d']} failed={list(a['errors']['insights_28d'])} "
              f"media_fail={ {k: v['posts'] for k, v in a['media_insight_failures'].items()} }", file=sys.stderr)


if __name__ == "__main__":
    main()
