#!/usr/bin/env python3
"""Continuous clip pipeline: detect changed sources -> tracker (fill-only) -> KPI reports + dashboard data.

Order is always TRACKER FIRST, THEN DASHBOARD. Idempotent: unchanged inputs are a no-op,
and fill-only writes never touch a non-blank cell, so re-running applies nothing new.

Sources watched (hashed into state/pipeline_state.json):
  * the live Clip Log and Posted - Unmatched tabs (public gviz CSV, no creds needed to read)
  * every file in data/sources/ (e.g. Instagram export reels.json / posts_*.json)

Tracker writes need GOOGLE_ACCESS_TOKEN (minted in CI from the Studio 145 service account).
Without it the pipeline runs in proposal mode: changes go to state/proposed_changes.csv only.
"""
import argparse, csv, glob, hashlib, json, os, re, sys, urllib.request
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clip_kpi as K

ROOT = K.ROOT
STATE = os.path.join(ROOT, "state")
SC = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
MATCH_TOLERANCE_S = 120  # IG shortcode time vs export creation_timestamp


def log(msg):
    os.makedirs(STATE, exist_ok=True)
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    print(line)
    with open(os.path.join(STATE, "pipeline.log"), "a") as f:
        f.write(line + "\n")


def sha(b):
    return hashlib.sha256(b).hexdigest()


def fetch_tab(cfg, tab):
    with urllib.request.urlopen(K.gviz_url(cfg["sheet_id"], cfg["tabs"][tab]), timeout=60) as r:
        return r.read()


def shortcode_ts(sc):
    n = 0
    for c in sc[:11]:
        n = n * 64 + SC.index(c)
    return ((n >> 23) + 1314220021721) / 1000


def load_ig_export(paths):
    """Instagram 'Download your information' JSON: reels.json / posts_N.json."""
    items = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        groups = d.get("ig_reels_media", []) if isinstance(d, dict) else d
        for g in groups:
            for m in g.get("media", []):
                ts = m.get("creation_timestamp") or g.get("creation_timestamp")
                if ts:
                    title = (m.get("title") or g.get("title") or "").encode("latin-1", "ignore").decode("utf-8", "ignore")
                    items.append({"ts": ts, "caption": title})
    return items


def tracker_updates(unmatched_rows, items, account):
    """Fill-only proposals for Posted - Unmatched. Returns (changes, flags)."""
    changes, flags = [], []
    for i, r in enumerate(unmatched_rows, start=2):
        link = r.get("Post link", "")
        if r.get("Account", "").strip().lower() != account.lower():
            continue
        m = re.search(r"instagram\.com/(?:reel|p)/([A-Za-z0-9_-]+)", link)
        if not m:
            continue
        t = shortcode_ts(m.group(1))
        near = [x for x in items if abs(x["ts"] - t) <= MATCH_TOLERANCE_S]
        if len(near) != 1:
            if r.get("Caption", "").strip() == "" or r.get("Post date", "").strip() == "":
                flags.append({"row": i, "link": link, "reason": f"{len(near)} export matches within {MATCH_TOLERANCE_S}s"})
            continue
        x = near[0]
        want = {"Caption": x["caption"][:200],
                "Post date": datetime.fromtimestamp(x["ts"], timezone.utc).date().isoformat()}
        for col, val in want.items():
            if col in r and str(r[col]).strip() == "" and val:
                changes.append({"row": i, "column": col, "old": "", "new": val, "link": link})
    return changes, flags


def col_letter(idx):
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def apply_changes(cfg, tab, header, changes, token):
    body = {"valueInputOption": "RAW", "data": [
        {"range": f"'{cfg['tabs'][tab]}'!{col_letter(header.index(c['column']))}{c['row']}", "values": [[c["new"]]]}
        for c in changes]}
    req = urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{cfg['sheet_id']}/values:batchUpdate",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def write_csv(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clips-csv", help="use a local Clip Log CSV instead of the live sheet")
    ap.add_argument("--unmatched-csv", help="use a local Posted - Unmatched CSV instead of the live sheet")
    ap.add_argument("--force", action="store_true", help="run even if nothing changed")
    ap.add_argument("--today")
    a = ap.parse_args(argv)
    cfg = K.load_config()
    os.makedirs(STATE, exist_ok=True)
    state_path = os.path.join(STATE, "pipeline_state.json")
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}

    clips_b = open(a.clips_csv, "rb").read() if a.clips_csv else fetch_tab(cfg, "clips")
    unm_b = open(a.unmatched_csv, "rb").read() if a.unmatched_csv else fetch_tab(cfg, "unmatched")
    srcs = sorted(glob.glob(os.path.join(ROOT, "data", "sources", "*.json")))
    hashes = {"clips": sha(clips_b), "unmatched": sha(unm_b)}
    hashes.update({os.path.basename(p): sha(open(p, "rb").read()) for p in srcs})
    changed = sorted(k for k in set(hashes) | set(state.get("hashes", {})) if hashes.get(k) != state.get("hashes", {}).get(k))
    if not changed and not a.force:
        log("no source changes; nothing to do")
        return 0
    log(f"changed sources: {', '.join(changed) or '(forced)'}")

    # 1) TRACKER FIRST (fill-only)
    import io
    unm_rows = list(csv.DictReader(io.StringIO(unm_b.decode("utf-8"))))
    header = list(unm_rows[0].keys()) if unm_rows else []
    changes, flags = tracker_updates(unm_rows, load_ig_export(srcs), cfg["ig_export_account"]) if srcs else ([], [])
    write_csv(os.path.join(STATE, "proposed_changes.csv"), changes, ["row", "column", "old", "new", "link"])
    write_csv(os.path.join(STATE, "flagged_rows.csv"), flags, ["row", "link", "reason"])
    log(f"tracker: {len(changes)} fill-only changes proposed, {len(flags)} rows flagged for review")
    token = os.environ.get("GOOGLE_ACCESS_TOKEN")
    if changes and token:
        res = apply_changes(cfg, "unmatched", header, changes, token)
        log(f"tracker: applied {res.get('totalUpdatedCells')} cells")
        for c in changes:
            log(f"  set row {c['row']} {c['column']} = {c['new'][:60]!r}")
        if not a.unmatched_csv:
            unm_b = fetch_tab(cfg, "unmatched")
            hashes["unmatched"] = sha(unm_b)
        clips_b = open(a.clips_csv, "rb").read() if a.clips_csv else fetch_tab(cfg, "clips")
        hashes["clips"] = sha(clips_b)
    elif changes:
        log("tracker: GOOGLE_ACCESS_TOKEN not set -> proposal mode, nothing written")

    # 2) THEN DASHBOARD: KPI reports + data/kpi.json from the (updated) tracker
    rows = list(csv.DictReader(io.StringIO(clips_b.decode("utf-8"))))
    today = K.parse_date(a.today) if a.today else date.today()
    res = K.build(rows, cfg, today)
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    json.dump(res, open(os.path.join(ROOT, "reports", "clip_kpi.json"), "w"), indent=1, ensure_ascii=False)
    open(os.path.join(ROOT, "reports", "clip_kpi.md"), "w").write(K.to_markdown(res, cfg))
    dash = {k: res[k] for k in ("generated", "counts", "by_page", "by_platform", "by_album", "album_status")}
    json.dump(dash, open(os.path.join(ROOT, "data", "kpi.json"), "w"), indent=1, ensure_ascii=False)
    log(f"dashboard: reports regenerated {json.dumps(res['counts'])}")

    state = {"hashes": hashes, "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    json.dump(state, open(state_path, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
