#!/usr/bin/env python3
"""Clip KPI report for TCS clips (stdlib only).

Reads Clip Log rows (CSV export or live public gviz CSV of the TCS Clip Albums
Tracker), computes Resonance Rate, Resonance Reach, Clip Longevity and views per
clip, rolls up to page / platform / album, applies observation windows and the
all-metrics-fail rule, and writes reports/clip_kpi.md + reports/clip_kpi.json
plus data/kpi.json for the dashboard.

Missing inputs give "n/a" (None in JSON), never 0. No number is invented.
"""
import argparse, csv, io, json, os, statistics, sys, urllib.parse, urllib.request
from collections import defaultdict
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_config(path=None):
    with open(path or os.path.join(HERE, "kpi_config.json")) as f:
        return json.load(f)


def gviz_url(sheet_id, tab):
    return (f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&headers=1&sheet="
            + urllib.parse.quote(tab))


def read_rows(source, cfg, tab="clips"):
    """source: path to CSV, or 'sheet' for the live tracker."""
    if source == "sheet":
        with urllib.request.urlopen(gviz_url(cfg["sheet_id"], cfg["tabs"][tab]), timeout=60) as r:
            text = r.read().decode("utf-8")
    else:
        with open(source, encoding="utf-8") as f:
            text = f.read()
    return list(csv.DictReader(io.StringIO(text)))


def num(v):
    """Parse a tracker number. Blank / non-numeric -> None (never 0)."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s == "" or s.lower() in ("n/a", "na", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def split(v):
    return [p.strip() for p in str(v or "").split("|") if p.strip()]


def platform_of(link):
    l = link.lower()
    if "tiktok.com" in l:
        return "tiktok"
    if "instagram.com/reel" in l:
        return "instagram_reels"
    if "instagram.com/p/" in l:
        return "instagram_post"
    if "youtube.com/shorts" in l:
        return "youtube_shorts"
    if "youtube.com" in l or "youtu.be" in l:
        return "youtube_longform"
    if "x.com" in l or "twitter.com" in l:
        return "x"
    return "unknown"


def parse_date(s):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, AttributeError):
            pass
    return None


def band(rr, cfg):
    if rr is None:
        return None
    b = cfg["resonance_bands"]
    if rr < b["dim_below"]:
        return "Dim"
    if rr < b["steady_below"]:
        return "Steady"
    if rr < b["bright_below"]:
        return "Bright"
    return "Outlier"


def clip_metrics(row, cfg):
    links = split(row.get("Post link(s)"))
    accounts = split(row.get("Account(s)"))
    dates = [d for d in (parse_date(x) for x in split(row.get("Post date(s)"))) if d]
    platforms = sorted({platform_of(l) for l in links}) or ["unknown"]
    views = num(row.get("Views"))
    comments, shares, saves = num(row.get("Comments")), num(row.get("Shares / reposts")), num(row.get("Saves"))
    reach = num(row.get("Reach"))  # column does not exist yet -> always None today
    first_views = num(row.get("First post views"))  # per-post views not in tracker yet

    numer, partial = None, False
    if comments is not None and shares is not None:
        if saves is not None:
            numer = comments + shares + saves
        elif cfg["missing_saves_policy"]["compute_partial_without_saves"]:
            numer, partial = comments + shares, True
    rr = round(numer / views * 1000, 2) if numer is not None and views else None
    rreach = round(numer / reach * 1000, 2) if numer is not None and reach else None
    longevity = round(views / first_views, 2) if views is not None and first_views else None
    return {
        "clip": row.get("Filename", ""),
        "album": row.get("Source album", "") or "(no album)",
        "source_file": row.get("Source file", ""),
        "pages": accounts,
        "platforms": platforms,
        "first_post_date": min(dates).isoformat() if dates else None,
        "links": links,
        "views": views,
        "resonance_rate": rr,
        "resonance_rate_partial_no_saves": partial if rr is not None else None,
        "resonance_band": band(rr, cfg),
        "resonance_reach": rreach,
        "longevity": longevity,
        "tracker_resonance_rate": num(row.get("Resonance Rate")),
    }


def window_days(platforms, cfg):
    w = cfg["observation_windows_days"]
    return max(w.get(p, w["unknown"]) for p in platforms)


def judge(clips, cfg, today):
    # page median views, by primary page
    by_page = defaultdict(list)
    for c in clips:
        if c["views"] is not None and c["pages"]:
            by_page[c["pages"][0]].append(c["views"])
    medians = {p: statistics.median(v) for p, v in by_page.items()}
    f = cfg["fails"]
    for c in clips:
        d = parse_date(c["first_post_date"] or "")
        win = window_days(c["platforms"], cfg)
        c["window_days"] = win
        c["age_days"] = (today - d).days if d else None
        checks = {}
        if c["resonance_band"] is not None:
            checks["resonance_rate"] = c["resonance_band"] in cfg["resonance_bands"]["fail_bands"]
        if c["resonance_reach"] is not None:
            checks["resonance_reach"] = band(c["resonance_reach"], cfg) in cfg["resonance_bands"]["fail_bands"]
        med = medians.get(c["pages"][0]) if c["pages"] else None
        c["page_median_views"] = med
        if f["views_below_page_median"] and c["views"] is not None and med is not None:
            checks["views"] = c["views"] < med
        if c["longevity"] is not None:
            checks["longevity"] = c["longevity"] < f["longevity_below"]
        c["fail_checks"] = checks
        if d is None:
            c["status"] = "no_post_date"
        elif c["age_days"] < win:
            c["status"] = "too_early"
        elif len(checks) < f["min_metrics_for_verdict"]:
            c["status"] = "insufficient_data"
        elif all(checks.values()):
            c["status"] = "non_performing"
        else:
            c["status"] = "ok"
    return medians


def rollup(clips, key):
    g = defaultdict(list)
    for c in clips:
        for k in key(c):
            g[k].append(c)
    out = {}
    for k, cs in g.items():
        vs = [c for c in cs if c["views"] is not None]
        tv = sum(c["views"] for c in vs)
        rr_cs = [c for c in vs if c["resonance_rate"] is not None and c["views"]]
        rr_v = sum(c["views"] for c in rr_cs)
        out[k] = {
            "clips": len(cs),
            "total_views": tv if vs else None,
            "median_views": statistics.median([c["views"] for c in vs]) if vs else None,
            "avg_resonance_rate_views_weighted": round(sum(c["resonance_rate"] * c["views"] for c in rr_cs) / rr_v, 2) if rr_v else None,
            "status_counts": dict(sorted(defaultdict(int, {s: sum(1 for c in cs if c["status"] == s) for s in {c["status"] for c in cs}}).items())),
        }
    return dict(sorted(out.items(), key=lambda kv: -(kv[1]["total_views"] or 0)))


def album_status(clips, cfg, today):
    w = cfg["observation_windows_days"]["album_after_last_clip"]
    g = defaultdict(list)
    for c in clips:
        g[c["album"]].append(c)
    out = {}
    for a, cs in g.items():
        ds = [parse_date(c["first_post_date"]) for c in cs if c["first_post_date"]]
        last = max(ds) if ds else None
        judged = [c for c in cs if c["status"] in ("ok", "non_performing")]
        if last is None:
            st = "no_posts"
        elif (today - last).days < w:
            st = "too_early"
        elif judged and all(c["status"] == "non_performing" for c in judged):
            st = "non_performing"
        elif judged:
            st = "ok"
        else:
            st = "insufficient_data"
        out[a] = {"last_clip_posted": last.isoformat() if last else None, "status": st}
    return out


def fmt(v):
    return "n/a" if v is None else (f"{int(v):,}" if isinstance(v, float) and v.is_integer() else str(v))


def patterns(posted, roll_platform):
    lines = []
    for p, r in roll_platform.items():
        lines.append(f"- {p}: {r['clips']} clips, {fmt(r['total_views'])} views, views-weighted RR {fmt(r['avg_resonance_rate_views_weighted'])}")
    bands = defaultdict(int)
    for c in posted:
        bands[c["resonance_band"] or "n/a"] += 1
    lines.append("- Resonance bands: " + ", ".join(f"{k} {v}" for k, v in sorted(bands.items())))
    partial = sum(1 for c in posted if c["resonance_rate_partial_no_saves"])
    lines.append(f"- {partial} clips have RR computed without saves (Saves blank in tracker)")
    lines.append(f"- Resonance Reach computable for {sum(1 for c in posted if c['resonance_reach'] is not None)} clips; Longevity for {sum(1 for c in posted if c['longevity'] is not None)} (tracker lacks Reach / per-post views columns)")
    return lines


def build(rows, cfg, today):
    clips = [clip_metrics(r, cfg) for r in rows if split(r.get("Post link(s)"))]
    medians = judge(clips, cfg, today)
    res = {
        "generated": today.isoformat(),
        "formulas": {
            "resonance_rate": "(saves+shares+comments)/views*1000",
            "resonance_reach": "(saves+shares+comments)/reach*1000, only when reach exists",
            "longevity": "total network views / first post views",
        },
        "counts": {"rows_read": len(rows), "posted_clips": len(clips)},
        "page_median_views": medians,
        "by_page": rollup(clips, lambda c: c["pages"][:1] or ["(none)"]),
        "by_platform": rollup(clips, lambda c: c["platforms"]),
        "by_album": rollup(clips, lambda c: [c["album"]]),
        "album_status": album_status(clips, cfg, today),
        "clips": clips,
    }
    for s in ("too_early", "non_performing", "ok", "insufficient_data", "no_post_date"):
        res["counts"][s] = sum(1 for c in clips if c["status"] == s)
    return res


def to_markdown(res, cfg):
    n, cap = cfg["report"]["top_n"], cfg["report"]["list_cap"]
    cl = res["clips"]
    top = sorted([c for c in cl if c["resonance_rate"] is not None and c["status"] != "too_early"],
                 key=lambda c: (-c["resonance_rate"], -(c["views"] or 0)))[:n]
    topv = sorted([c for c in cl if c["views"] is not None], key=lambda c: -c["views"])[:n]
    row = lambda c: f"| {c['clip']} | {c['album']} | {','.join(c['pages'])} | {','.join(c['platforms'])} | {fmt(c['views'])} | {fmt(c['resonance_rate'])}{'*' if c['resonance_rate_partial_no_saves'] else ''} | {c['resonance_band'] or 'n/a'} | {fmt(c['resonance_reach'])} | {fmt(c['longevity'])} | {c['first_post_date'] or 'n/a'} |"
    hdr = "| clip | album | page | platform | views | RR | band | RReach | longev | posted |\n|---|---|---|---|---|---|---|---|---|---|"
    k = res["counts"]
    out = [f"# Clip KPI report — {res['generated']}", "",
           "For cutting agents (Leonard and others). RR = (saves+shares+comments)/views*1000. `*` = saves blank, RR computed without saves. n/a = input missing, not zero.", "",
           f"Posted clips: {k['posted_clips']} · ok {k['ok']} · too early {k['too_early']} · non-performing {k['non_performing']} · insufficient data {k['insufficient_data']} · no post date {k['no_post_date']}", "",
           f"## Top performers by Resonance Rate (top {n}, outside window only)", hdr] + [row(c) for c in top] + [
           "", f"## Top by views (top {n})", hdr] + [row(c) for c in topv]
    for s, title in (("too_early", "Too early (inside observation window)"), ("non_performing", "Non-performing (failed every available metric)")):
        cs = sorted([c for c in cl if c["status"] == s], key=lambda c: c["first_post_date"] or "")
        out += ["", f"## {title} — {len(cs)}" + (f" (showing {cap})" if len(cs) > cap else ""), hdr] + [row(c) for c in cs[:cap]]
    alb = [(a, s) for a, s in res["album_status"].items() if s["status"] in ("too_early", "non_performing")]
    out += ["", "## Albums", "| album | last clip posted | status |", "|---|---|---|"] + [f"| {a} | {s['last_clip_posted']} | {s['status']} |" for a, s in alb]
    out += ["", "## Patterns"] + patterns(cl, res["by_platform"])
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="sheet", help="Clip Log CSV path, or 'sheet' for the live tracker")
    ap.add_argument("--config")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--outdir", default=ROOT)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    today = parse_date(a.today) if a.today else date.today()
    res = build(read_rows(a.source, cfg), cfg, today)
    os.makedirs(os.path.join(a.outdir, "reports"), exist_ok=True)
    os.makedirs(os.path.join(a.outdir, "data"), exist_ok=True)
    with open(os.path.join(a.outdir, "reports", "clip_kpi.json"), "w") as f:
        json.dump(res, f, indent=1, ensure_ascii=False)
    with open(os.path.join(a.outdir, "reports", "clip_kpi.md"), "w") as f:
        f.write(to_markdown(res, cfg))
    dash = {k: res[k] for k in ("generated", "counts", "by_page", "by_platform", "by_album", "album_status")}
    with open(os.path.join(a.outdir, "data", "kpi.json"), "w") as f:
        json.dump(dash, f, indent=1, ensure_ascii=False)
    print(json.dumps(res["counts"]))
    return res


if __name__ == "__main__":
    main()
