"""Interactive HTML report - one self-contained, CMS-agnostic file.

Every judged suggestion is embedded as data; the page applies the thresholds in the browser, so
the filters (score, context, anchor, value, cannibalisation and page similarity in %, links per
page, match type, search) change the selection live. It opens with the thresholds used by the run,
so its default selection matches links.csv.

The Graph view draws the whole site with force-graph (vendor/, inlined so the file works offline):
pages sized by their links in, the links in content today, the new links and - with embeddings -
a topic map, the similarity of every link and the closest pages that are not linked yet. Clicking
a page zooms in on it: its linked pages gather around it (the more similar, the closer) and the
rest of the site moves aside. `internal-linking report` rebuilds the file from saved results.
"""
import json
from pathlib import Path

import numpy as np

from . import agent, embeddings, verify
from .anchors import detect_lang
from .common import clean_title, data_dir, is_archive, norm_url, now, read_csv, read_jsonl, site_suffixes
from .config import load_config
from .crawl import strip_boilerplate, strip_excerpts

NUMERIC = {"score": "sc", "jev_context": "cx", "jev_anchor": "an", "jev_value": "va", "jev_cannibalisation": "ca",
           "similarity": "si"}
GRAPH_LIB = Path(__file__).with_name("vendor") / "force-graph.min.js"
NO_LIMIT = 31  # the "links to one page" slider's last stop


def _num(value) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _compact(r: dict) -> dict:
    row = {"s": r["source_url"], "st": r.get("source_title", ""), "t": r["target_url"],
           "tt": r.get("target_title", ""), "a": r["anchor"], "ty": r.get("anchor_type", ""),
           "pl": r.get("placement", ""), "c": r.get("context", ""), "v": r.get("jev_verdict", ""),
           "l": r.get("lang", ""), "m": int(r.get("in_menu") or 0)}
    for field, key in NUMERIC.items():
        row[key] = _num(r.get(field))
    if r.get("keyword"):
        row["k"] = r["keyword"]
    return row


def page_summary(pages: list[dict], cfg: dict | None = None) -> dict:
    """URL → title; `in`: other pages linking to it from their content today (template links -
    menu, footer, sidebars - excluded); `any`: other pages linking to it from anywhere, menu and
    breadcrumbs included (0 = a true orphan, reachable only from the sitemap); `out`: pages it links
    to from its content (only crawled pages count); whether it is a listing or an archive."""
    strip_boilerplate(pages)  # idempotent; older saved crawls may still hold template blocks
    strip_excerpts(pages)
    crawled = {norm_url(p["url"]) for p in pages}
    inbound: dict[str, set[str]] = {}
    anywhere: dict[str, set[str]] = {}
    for p in pages:
        u = norm_url(p["url"])
        for link in p.get("content_links", []):
            inbound.setdefault(link, set()).add(u)
        for link in {*p.get("content_links", []), *p.get("breadcrumb_links", []), *p.get("other_links", [])}:
            anywhere.setdefault(link, set()).add(u)
    out = {}
    for p in pages:
        u = norm_url(p["url"])
        out[p["url"]] = {"title": p.get("title", ""), "in": len(inbound.get(u, set()) - {u}),
                         "any": len(anywhere.get(u, set()) - {u}),
                         "out": len(set(p.get("content_links", [])) & crawled - {u})}
        if p.get("listing"):
            out[p["url"]]["listing"] = 1
        if cfg and is_archive(p["url"], cfg):
            out[p["url"]]["archive"] = 1
    return out


def link_graph(pages: list[dict], vectors: dict[str, np.ndarray]) -> dict:
    """The site as a graph for the Graph view: every page (with its place on the topic map when
    there are embeddings), the links in content between crawled pages today with the similarity of
    the two pages, and per page the closest pages it is not linked with in either direction."""
    strip_boilerplate(pages)
    strip_excerpts(pages)
    suffixes = site_suffixes(pages)
    index = {norm_url(p["url"]): i for i, p in enumerate(pages)}
    nodes = [{"u": p["url"], "t": clean_title(p.get("title") or p.get("h1") or "", suffixes), "l": detect_lang(p)}
             for p in pages]
    links, linked = [], [set() for _ in pages]
    for i, p in enumerate(pages):
        for j in sorted({index[u] for u in p.get("content_links", []) if u in index} - {i}):
            links.append([i, j, embeddings.similarity(vectors, p["url"], pages[j]["url"])])
            linked[i].add(j)
            linked[j].add(i)
    near: list[list] = [[] for _ in pages]
    have = [i for i, p in enumerate(pages) if p["url"] in vectors]
    if len(have) >= 2:
        m = np.stack([vectors[pages[i]["url"]] for i in have])
        for (x, y), i in zip(embeddings.topic_map(m), have):
            nodes[i]["x"], nodes[i]["y"] = round(float(x), 4), round(float(y), 4)
        pos = {i: n for n, i in enumerate(have)}
        skip = [{pos[j] for j in linked[i] if j in pos} for i in have]
        for i, best in zip(have, embeddings.nearest(m, embeddings.NEAREST, skip, [nodes[i]["l"] for i in have])):
            near[i] = [[have[j], s] for j, s in best]
    return {"nodes": nodes, "links": links, "near": near}


def write_report(path: Path, domain: str, rows: list[dict], cfg: dict, max_links: int,
                 stats: dict | None = None, pages: list[dict] | None = None,
                 vectors: dict[str, np.ndarray] | None = None) -> None:
    vectors = vectors or {}
    run = json.loads((path.parent / "run.json").read_text(encoding="utf-8")) if (path.parent / "run.json").exists() else {}
    embeddings.add_similarity(rows, vectors)  # also fills it in for results saved before embeddings
    model, dims = embeddings.settings()
    payload = {
        "domain": domain,
        "run_at": run.get("run_at", ""),  # when `run` made the suggestions
        "built_at": now(),                # when this file was written (a rebuild: later)
        "defaults": {"score": round(cfg["score_min"] * 100), "ctx": round(cfg["context_min"] * 100),
                     "anc": round(cfg["anchor_min"] * 100), "val": 0,
                     "can": round(cfg["cannibalisation_max"] * 100),
                     "sim": round(cfg.get("similarity_min", 0) * 100), "max": max_links,
                     "maxt": min(cfg.get("max_links_per_target") or NO_LIMIT, NO_LIMIT)},
        "no_limit": NO_LIMIT,
        "stats": stats or run.get("stats", {}),
        "embeddings": f"{model}, {dims} dimensions" if vectors else "",
        "agent": agent.guidelines(domain),
        "verify": [{"st": v["status"], "s": v["source_url"], "a": v["anchor"], "t": v["target_url"],
                    "f": v.get("found_text", ""), "n": v.get("note", ""), "c": v.get("checked", ""), "w": v.get("where", "")}
                   for v in verify.load(path.parent)],
        "version": agent.__version__,
        "rows": [_compact(r) for r in rows],
        "pages": page_summary(pages, cfg) if pages else {},
        "graph": link_graph(pages, vectors) if pages else {"nodes": [], "links": [], "near": []},
    }
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    # the library first: the data goes in last, so nothing in it is ever taken for a placeholder
    html = (TEMPLATE.replace("__NO_LIMIT__", str(NO_LIMIT))
            .replace("__GRAPH_LIB__", GRAPH_LIB.read_text(encoding="utf-8")).replace("__DATA__", data))
    path.write_text(html, encoding="utf-8")


def rebuild(args) -> None:
    """Rebuilds report.html from data/<domain>/recommendations.csv - no crawl, no Jev calls."""
    cfg = load_config(args.config)
    missing = 0
    for domain in args.domain:
        ddir = data_dir(domain)
        src = ddir / "recommendations.csv"
        if not src.exists():
            print(f"! {domain}: no {src} - run `internal-linking run --domain {domain}` first")
            missing += 1
            continue
        rows = read_csv(src)
        pages = read_jsonl(ddir / "pages.jsonl")
        vectors, _ = embeddings.embed_pages(pages, ddir, compute=False)  # cached only - no API calls
        write_report(ddir / "report.html", domain, rows, cfg, args.max_links or cfg["max_links_per_page"],
                     pages=pages, vectors=vectors)
        print(f"{domain}: {len(rows)} suggestions, {len(vectors)} pages with embeddings → {ddir / 'report.html'}")
    if missing:
        raise SystemExit(1)


def add_parser(sub) -> None:
    p = sub.add_parser("report", help="rebuild report.html from saved results (no crawl, no Jev calls)")
    p.add_argument("--domain", action="append", required=True, help="domain (repeatable)")
    p.add_argument("--max-links", type=int, help="default links-per-page filter (default from config)")
    p.set_defaults(func=rebuild)


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Internal links report</title>
<script>try { const t = localStorage.getItem("il-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) {}</script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root { --bg:#ffffff; --panel:#ffffff; --ink:#0a0a0a; --muted:#5e5e66; --line:#e6e4ee; --soft:#f6f5fb;
  --accent:#5b2ee0; --accent-btn:#5b2ee0; --accent-soft:#eeeafd; --mark:#d4f94c; --mark-ink:#0a0a0a;
  --good:#5b2ee0; --warn:#c98a12; --bad:#d23c2a; --shadow:0 1px 2px rgba(10,10,10,.05);
  --g-node:#8b8996; --g-today:rgba(94,94,102,.32); --g-new:#5b2ee0; --g-sim:#1baf7a; --g-fade:rgba(94,94,102,.07);
  --display:"Space Grotesk",system-ui,sans-serif; --body:"Manrope",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
:root { color-scheme:light }
/* dark: follows the system unless the viewer picked a theme (data-theme on <html>) */
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#0a0a0a; --panel:#141414; --ink:#f4f4f5; --muted:#a1a1aa;
  --line:#2a2a30; --soft:#1c1c1f; --accent:#9b7fff; --accent-soft:#221a3d; --good:#9b7fff; --warn:#e0a93a;
  --bad:#f0705c; --shadow:none; color-scheme:dark;
  --g-node:#7c7a88; --g-today:rgba(161,161,170,.28); --g-new:#8f72fa; --g-sim:#199e70; --g-fade:rgba(161,161,170,.06); } }
:root[data-theme="dark"] { --bg:#0a0a0a; --panel:#141414; --ink:#f4f4f5; --muted:#a1a1aa;
  --line:#2a2a30; --soft:#1c1c1f; --accent:#9b7fff; --accent-soft:#221a3d; --good:#9b7fff; --warn:#e0a93a;
  --bad:#f0705c; --shadow:none; color-scheme:dark;
  --g-node:#7c7a88; --g-today:rgba(161,161,170,.28); --g-new:#8f72fa; --g-sim:#199e70; --g-fade:rgba(161,161,170,.06); }
* { box-sizing:border-box } html,body { margin:0 }
body { background:var(--bg); color:var(--ink); font:15px/1.55 var(--body) }
h1, h3, .stat b, .brand { font-family:var(--display); letter-spacing:-.01em }
a { color:var(--accent); text-decoration:none } a:hover { text-decoration:underline }
header { padding:22px 20px 8px; max-width:1320px; margin:0 auto; display:flex; justify-content:space-between;
  align-items:flex-start; gap:16px; flex-wrap:wrap }
.theme { display:inline-flex; border:1px solid var(--line); border-radius:999px; padding:3px; background:var(--panel) }
.theme button { font:inherit; font-size:13px; border:0; background:transparent; color:var(--muted); padding:5px 12px;
  border-radius:999px; cursor:pointer } .theme button.on { background:var(--ink); color:var(--bg); font-weight:600 }
h1 { font-size:26px; margin:0; font-weight:700 } h1 .dot { color:var(--accent) } .sub { color:var(--muted); margin:2px 0 0; font-size:14px }
.layout { display:grid; grid-template-columns:290px 1fr; gap:20px; max-width:1320px; margin:0 auto; padding:12px 20px 60px }
aside { position:sticky; top:12px; align-self:start; background:var(--panel); border:1px solid var(--line);
  border-radius:14px; padding:16px; box-shadow:var(--shadow); max-height:calc(100vh - 24px); overflow:auto }
aside h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 10px }
.f { margin-bottom:14px } .f label { display:flex; justify-content:space-between; font-size:13px; font-weight:600 }
.f label span { color:var(--accent); font-variant-numeric:tabular-nums } .f small { color:var(--muted); font-size:12px; display:block }
input[type=range] { width:100%; accent-color:var(--accent) }
select, input[type=search] { width:100%; font:inherit; font-size:14px; padding:7px 9px; border-radius:8px;
  border:1px solid var(--line); background:var(--soft); color:var(--ink) }
.btn { font:inherit; font-size:13px; border:1px solid var(--line); background:var(--soft); color:var(--ink);
  border-radius:8px; padding:6px 10px; cursor:pointer } .btn:hover { border-color:var(--accent) }
.btn.primary { background:var(--accent-btn); color:#fff; border-color:var(--accent-btn); font-weight:600 }
.btn.primary:hover { filter:brightness(1.1) }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin-bottom:14px }
.stat { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:10px 14px; box-shadow:var(--shadow) }
.stat:first-child { background:var(--accent-btn); border-color:var(--accent-btn); color:#fff } .stat:first-child span { color:#e4dcff }
.stat b { display:block; font-size:22px; font-variant-numeric:tabular-nums } .stat span { color:var(--muted); font-size:13px }
.tabs { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:14px; align-items:center }
.tab { font:inherit; font-size:14px; border:1px solid var(--line); background:var(--panel); color:var(--ink);
  padding:7px 12px; border-radius:999px; cursor:pointer } .tab.on { background:var(--accent-btn); color:#fff; border-color:var(--accent-btn) }
.tabs .right { margin-left:auto; display:flex; gap:6px }
.group { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:16px 18px;
  margin-bottom:14px; box-shadow:var(--shadow) }
.group-h { display:flex; justify-content:space-between; gap:12px; align-items:baseline; flex-wrap:wrap }
.group-h h3 { font-size:16px; margin:0 } .count { color:var(--muted); font-size:13px; white-space:nowrap }
.url { font:12px ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); word-break:break-all }
.item { border-top:1px solid var(--line); margin-top:14px; padding-top:14px }
details.group > summary { list-style:none; cursor:pointer } details.group > summary::-webkit-details-marker { display:none }
details.group > summary .chev { display:inline-block; transition:transform .15s; color:var(--muted); margin-right:6px }
details.group[open] > summary .chev { transform:rotate(90deg) }
.compact { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px }
.compact span { font-size:13px; background:var(--soft); border:1px solid var(--line); border-radius:8px; padding:3px 9px }
.compact b { font-weight:600 } .compact span.arrow { color:var(--muted); margin:0 4px; background:none; border:0; padding:0 }
.badge { display:inline-block; font-size:12px; font-weight:600; border-radius:999px; padding:2px 9px; margin-left:8px; vertical-align:2px }
.badge.orphan { background:#fde2de; color:#8f2418 } .badge.weak { background:#fdf1d6; color:#7a520c }
.badge.new { background:var(--accent-soft); color:var(--accent) }
.sort { width:auto; font-size:13px; padding:6px 8px }
table.pl { width:100%; border-collapse:collapse; font-size:14px } .pl th, .pl td { text-align:left; padding:9px 10px 9px 0;
  border-bottom:1px solid var(--line); vertical-align:top } .pl th { color:var(--muted); font-weight:500; font-size:12px; text-transform:uppercase }
.pl td.n { font-variant-numeric:tabular-nums; white-space:nowrap } .pl .plus { color:var(--accent); font-weight:700 }
.linkish { background:none; border:0; padding:0; font:inherit; color:var(--accent); cursor:pointer; font-size:13px }
.steps { display:grid; grid-template-columns:auto 1fr; gap:6px 12px; align-items:start }
.k { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); padding-top:3px; white-space:nowrap }
.ctx { background:var(--soft); border-radius:10px; padding:10px 12px }
mark { background:var(--mark); color:var(--mark-ink); border-radius:4px; padding:1px 3px; font-weight:700 }
.anchor { font-weight:700; background:var(--accent-soft); border-radius:6px; padding:1px 7px } .pill { display:inline-block; font-size:11px; border:1px solid var(--line); border-radius:999px;
  padding:1px 8px; color:var(--muted); margin-left:6px; vertical-align:1px }
.actions { display:flex; gap:6px; flex-wrap:wrap; margin-top:10px }
.metrics { display:flex; gap:14px; flex-wrap:wrap; margin-top:10px }
.m { font-size:12px; color:var(--muted); min-width:92px } .m b { color:var(--ink); font-variant-numeric:tabular-nums }
.bar { height:5px; background:var(--line); border-radius:3px; overflow:hidden; margin-top:3px }
.bar i { display:block; height:100% } .ok { background:var(--good) } .mid { background:var(--warn) } .low { background:var(--bad) }
.rej { display:grid; grid-template-columns:1fr auto; gap:4px 12px; padding:9px 0; border-top:1px solid var(--line); font-size:14px }
.reason { color:var(--bad); font-size:13px; white-space:nowrap } .muted { color:var(--muted) }
.chips { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:12px } .chip.v.on { border-color:var(--accent); color:var(--accent) } .chip { font-size:12px; background:var(--soft);
  border:1px solid var(--line); border-radius:999px; padding:3px 10px; cursor:pointer } .chip.on { border-color:var(--bad); color:var(--bad) }
table.kw { width:100%; border-collapse:collapse; font-size:14px } .kw th, .kw td { text-align:left; padding:8px 10px 8px 0;
  border-bottom:1px solid var(--line) } .kw th { color:var(--muted); font-weight:500; font-size:12px; text-transform:uppercase }
.empty { color:var(--muted); padding:30px; text-align:center } .more { text-align:center; margin:10px 0 }
.toast { position:fixed; bottom:18px; left:50%; transform:translateX(-50%); background:var(--ink); color:var(--bg);
  padding:8px 14px; border-radius:8px; font-size:13px; opacity:0; transition:opacity .2s; pointer-events:none } .toast.show { opacity:1 }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) .badge.orphan { background:#4a1d17; color:#ffb4a8 }
  :root:not([data-theme="light"]) .badge.weak { background:#3d2e0e; color:#f5cf7a } }
:root[data-theme="dark"] .badge.orphan { background:#4a1d17; color:#ffb4a8 } :root[data-theme="dark"] .badge.weak { background:#3d2e0e; color:#f5cf7a }
/* graph view */
.gbar { display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center; margin-bottom:12px }
.seg { display:inline-flex; border:1px solid var(--line); border-radius:999px; padding:3px; background:var(--panel) }
.seg button { font:inherit; font-size:13px; border:0; background:transparent; color:var(--muted); padding:5px 12px;
  border-radius:999px; cursor:pointer } .seg button.on { background:var(--ink); color:var(--bg); font-weight:600 }
.seg button:disabled { opacity:.45; cursor:not-allowed }
.chk { display:inline-flex; gap:6px; align-items:center; font-size:13px; cursor:pointer; white-space:nowrap }
.chk input { accent-color:var(--accent); margin:0 }
.gbar input[type=search] { width:200px; max-width:100% } .gbar .sort { width:auto }
.gstage { position:relative; height:min(74vh,780px); min-height:440px; background:var(--panel); border:1px solid var(--line);
  border-radius:14px; overflow:hidden; box-shadow:var(--shadow); margin-bottom:14px }
#graph { position:absolute; inset:0 }
.glegend { position:absolute; left:12px; bottom:12px; display:flex; flex-wrap:wrap; gap:4px 14px; max-width:calc(100% - 24px);
  font-size:12px; color:var(--muted); background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:7px 10px;
  pointer-events:none; opacity:.94 }
.glegend span { display:inline-flex; align-items:center; gap:6px; white-space:nowrap }
.sw { width:10px; height:10px; border-radius:50%; display:inline-block }
.key { width:20px; height:0; border-top:2px solid; display:inline-block; vertical-align:middle } .key.dash { border-top-style:dashed }
.ghint { position:absolute; left:12px; top:10px; font-size:12px; color:var(--muted); max-width:min(420px, 60%); pointer-events:none }
.focus { position:absolute; top:12px; right:12px; bottom:12px; width:340px; overflow:auto; background:var(--panel);
  border:1px solid var(--line); border-radius:12px; padding:14px 16px; box-shadow:0 8px 24px rgba(10,10,10,.14) }
.focus h3 { font-size:16px; margin:0 30px 2px 0 } .focus .x { position:absolute; top:10px; right:10px; padding:3px 8px }
.fstats { display:grid; grid-template-columns:repeat(2,1fr); gap:6px; margin:12px 0 4px }
.fstats div { background:var(--soft); border-radius:8px; padding:6px 9px; font-size:12px; color:var(--muted) }
.fstats b { display:block; font-size:17px; color:var(--ink) } .fstats b.plus { color:var(--accent) }
.fsec { margin-top:14px } .fsec h4 { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted);
  margin:0 0 4px; display:flex; align-items:center; gap:8px; font-weight:600 }
.frow { display:grid; grid-template-columns:46px 1fr; gap:10px; align-items:center; width:100%; text-align:left; background:none;
  border:0; border-top:1px solid var(--line); padding:6px 0; font:inherit; font-size:13px; color:var(--ink); cursor:pointer }
.frow:hover .ft { color:var(--accent) } .frow .fa { display:block; color:var(--muted); font-size:12px }
.fsim { font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums } .fsim .bar { margin-top:3px }
.gcards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-bottom:14px }
.gcards .stat:first-child { background:var(--panel); border-color:var(--line); color:var(--ink) }
.gcards .stat:first-child span { color:var(--muted) } .gcards .stat b { font-variant-numeric:normal }
.gcharts { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:0 14px }
.gcharts details.tbl, .gcharts > p { grid-column:1 / -1 }
.chart h4 { margin:0; font-size:14px; font-family:var(--display) } .chart p { margin:2px 0 8px; color:var(--muted); font-size:12px }
.chart svg { width:100%; height:auto; display:block; overflow:visible } .chart .grid { stroke:var(--line); stroke-width:1 }
.chart text { fill:var(--muted); font-size:12px; font-family:var(--body); font-variant-numeric:tabular-nums } .chart .hit { fill:transparent; cursor:default }
.chart .hit:hover + path, .chart path.on { filter:brightness(1.15) }
details.tbl { margin-top:10px; font-size:13px } details.tbl summary { cursor:pointer; color:var(--accent) }
details.tbl table { margin-top:8px } details.tbl td { font-variant-numeric:tabular-nums }
.tip { position:fixed; z-index:20; pointer-events:none; background:var(--panel); color:var(--ink); border:1px solid var(--line);
  border-radius:8px; padding:6px 9px; font-size:12px; box-shadow:0 4px 14px rgba(10,10,10,.14); display:none; max-width:300px }
.tip b { font-size:14px; display:block }
.force-graph-container .float-tooltip-kap { font:12px/1.45 var(--body); color:var(--ink); background:var(--panel);
  border:1px solid var(--line); border-radius:8px; padding:6px 9px; box-shadow:0 4px 14px rgba(10,10,10,.14); max-width:320px }
.float-tooltip-kap b { font-size:13px } .float-tooltip-kap .url { display:block }
@media (max-width: 860px) { .layout { grid-template-columns:1fr } aside { position:static; max-height:none }
  .gstage { height:66vh; min-height:380px } .ghint { display:none }
  .focus { top:auto; left:8px; right:8px; bottom:8px; width:auto; max-height:46% } }
</style></head><body>
<header><div><h1>Internal links to insert<span class="dot">.</span></h1><p class="sub" id="sub"></p></div>
  <div class="theme" role="group" aria-label="Theme"><button data-theme-set="light">☀ Light</button><button data-theme-set="dark">☾ Dark</button></div></header>
<div class="layout">
<aside>
  <h2>Filters</h2>
  <div class="f"><label>Overall score <span id="v-score"></span></label><input type="range" min="0" max="100" id="f-score">
    <small>Jev's probability that the link is good</small></div>
  <div class="f"><label>Context <span id="v-ctx"></span></label><input type="range" min="0" max="100" id="f-ctx">
    <small>the passage is about the target's topic</small></div>
  <div class="f"><label>Anchor quality <span id="v-anc"></span></label><input type="range" min="0" max="100" id="f-anc">
    <small>the phrase is natural link text</small></div>
  <div class="f"><label>Link value <span id="v-val"></span></label><input type="range" min="0" max="100" id="f-val">
    <small>usefulness to the reader at this spot</small></div>
  <div class="f"><label>Cannibalisation below <span id="v-can"></span></label><input type="range" min="0" max="100" id="f-can">
    <small>reject when the phrase is the source page's own topic</small></div>
  <div class="f" id="f-sim-wrap"><label>Page similarity <span id="v-sim"></span></label><input type="range" min="0" max="100" id="f-sim">
    <small>how close the two pages are in meaning (embeddings)</small></div>
  <div class="f"><label>Links per page <span id="v-max"></span></label><input type="range" min="1" max="15" id="f-max"></div>
  <div class="f"><label>Links to one page <span id="v-maxt"></span></label><input type="range" min="1" max="__NO_LIMIT__" id="f-maxt">
    <small>new links to the same target - the best ones win</small></div>
  <div class="f"><label>Match type</label><select id="f-type"><option value="all">exact + partial</option>
    <option value="exact">exact only</option><option value="partial">partial only</option></select></div>
  <div class="f" id="f-lang-wrap"><label>Language</label><select id="f-lang"></select></div>
  <div class="f" id="f-kw-wrap"><label>Keyword</label><select id="f-kw"></select></div>
  <div class="f"><label>Search</label><input type="search" id="f-q" placeholder="page, target or phrase"></div>
  <div class="actions"><button class="btn" id="reset">Reset to run defaults</button></div>
</aside>
<main>
  <div class="stats" id="stats"></div>
  <div class="tabs"><button class="tab on" data-view="out" title="for each page: the pages it will link to">Linking to</button>
    <button class="tab" data-view="in" title="for each page: the pages it will get links from">Linked from</button>
    <button class="tab" data-view="pagelist" title="every page with its links today and after the changes">Pages</button>
    <button class="tab" data-view="graph" id="tab-graph" title="the whole site as a graph: pages, links today, new links, similarity">Graph</button>
    <button class="tab" data-view="verify" id="tab-verify" title="are the links handed out inserted? (internal-linking verify)">Verification</button>
    <button class="tab" data-view="keywords" id="tab-kw">Keywords</button><button class="tab" data-view="rejected">Rejected</button>
    <div class="right"><select id="sort" class="sort" aria-label="Sort">
      <option value="count">Most new links</option><option value="weak">Weakest linked today</option><option value="az">A-Z</option></select>
      <button class="btn" id="toggle-all">Expand all</button><button class="btn" id="md" title="the selected links with instructions for a coding agent (Claude Code, Antigravity...) that inserts them in the site's source; `internal-linking verify` checks the result">For AI agent (.md)</button><button class="btn primary" id="csv">Download CSV</button></div></div>
  <div id="view"></div>
  <section id="graph-wrap" hidden>
    <div class="gbar">
      <div class="seg" role="group" aria-label="Arrange pages"><button data-arrange="topics" title="pages close in meaning sit close together (embeddings); how far apart two groups sit means little - read the similarity values">By topic</button><button data-arrange="links" title="linked pages pull together">By links</button></div>
      <label class="chk"><input type="checkbox" id="g-today"> Links today</label>
      <label class="chk"><input type="checkbox" id="g-new"> New links</label>
      <label class="chk" id="g-sim-wrap"><input type="checkbox" id="g-sim"> Similar, not linked</label>
      <select id="g-set" class="sort" aria-label="Pages shown"><option value="all">All pages</option><option value="new">Pages with new links</option></select>
      <input type="search" id="g-find" list="g-titles" placeholder="Find a page and zoom in" aria-label="Find a page"><datalist id="g-titles"></datalist>
      <button class="btn" id="g-fit">Fit to screen</button>
    </div>
    <div class="gstage">
      <div id="graph"></div>
      <div class="ghint" id="g-hint">Click a page to zoom in on it: its linked pages gather around it - the more similar, the closer - and the rest of the site moves aside. Esc or a click on the background goes back.</div>
      <div class="glegend" id="g-legend"></div>
      <aside class="focus" id="focus" hidden></aside>
    </div>
    <div class="gcards" id="g-cards"></div>
    <div class="gcharts" id="g-charts"></div>
  </section>
</main>
</div>
<div class="toast" id="toast">Copied</div>
<div class="tip" id="tip" role="tooltip"></div>
<script type="application/json" id="data">__DATA__</script>
<script>__GRAPH_LIB__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const rows = D.rows;
const NO_LIMIT = D.no_limit;  // the "links to one page" slider's last stop
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const path = u => { try { return new URL(u).pathname || "/"; } catch (e) { return u; } };
const pct = x => x == null ? "–" : Math.round(x * 100) + "%";
const REASONS = { cannibalisation:"the phrase is the source page's own topic", off_topic:"the passage is about another topic",
  weak_anchor:"not natural link text", low_value:"link value below the filter", low_score:"overall score below the filter",
  low_similarity:"the two pages are far apart in meaning (embeddings)",
  too_generic:"phrase too generic", wrong_intent:"reader expects another kind of page here",
  navigation_or_template:"list, navigation or repeated block", limit:"per-page limit - better links won", target_limit:"per-target limit - better links to this page won",
  duplicate_phrase:"this phrase already carries a better link", not_judged:"not judged by Jev (API error)",
  filtered:"hidden by type, language, keyword or search filter" };
const KEY = "il-filters:" + D.domain;
const langs = [...new Set(rows.map(r => r.l).filter(Boolean))];
const keywords = [...new Set(rows.map(r => r.k).filter(Boolean))].sort();
let S = { ...D.defaults, type:"all", lang:"all", kw:"all", q:"", view:"out", sort:"count", hidden:{} };
try { Object.assign(S, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch (e) {}
if (!["out", "in", "pagelist", "graph", "verify", "keywords", "rejected"].includes(S.view)) S.view = S.view === "targets" ? "in" : "out";
const inbound = u => (D.pages[u] || {}).in;
// how a page is linked today: "orphan" - no internal link at all (only the sitemap knows it);
// "none" - no links in other pages' content, only menu, footer or breadcrumbs; "weak" - 1-2 in content
function linkState(u) {
  const p = D.pages[u];
  if (!p) return null;
  return p.any === 0 ? "orphan" : p.in === 0 ? "none" : p.in <= 2 ? "weak" : "ok";
}
const pageKind = u => { const p = D.pages[u] || {};
  return (p.listing ? '<span class="pill" title="shows excerpts of other pages - never a link source">listing</span>' : "") +
    (p.archive ? '<span class="pill" title="tag, author or date archive - never a link target">archive</span>' : ""); };
const weakness = u => { const p = D.pages[u]; return !p ? 1e9 : p.any === 0 ? -1 : p.in; };  // orphans first
const STATE = { orphan: ["orphan", "orphan · no internal links", "no page links to it - not even the menu; only the sitemap"],
  none: ["weak", "no links in content", "linked only from the menu, footer or breadcrumbs"],
  weak: ["weak", "weak", "1-2 links in other pages' content"] };
const stateBadge = (u, long) => { const s = STATE[linkState(u)];
  return s ? `<span class="badge ${s[0]}" title="${esc(s[2])}">${esc(long ? s[1] : s[1].split(" · ")[0])}</span>` : ""; };
const openGroups = new Set();
let expandAll = false;

const timeOf = s => String(s || "").slice(0, 16).replace("T", " ");  // 2026-10-11T13:05+02:00 → 2026-10-11 13:05
const stamp = () => { const d = new Date(), p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`; };
$("sub").textContent = D.domain + " · " + (D.run_at ? "run " + timeOf(D.run_at) : "report " + timeOf(D.built_at)) +
  (D.run_at && timeOf(D.run_at) !== timeOf(D.built_at) ? " · report rebuilt " + timeOf(D.built_at) : "") +
  " · " + rows.length + " suggestions judged" +
  (D.stats["Jev cost"] ? " · Jev cost " + D.stats["Jev cost"] : "") +
  (D.embeddings ? " · embeddings: " + D.embeddings + (D.stats["Embeddings cost"] ? " (" + D.stats["Embeddings cost"] + ")" : "") : "");
if (!rows.some(r => r.si != null)) $("f-sim-wrap").hidden = true;
if (!(D.graph && D.graph.nodes.length)) { $("tab-graph").hidden = true; if (S.view === "graph") S.view = "out"; }
if (!D.verify.length) { $("tab-verify").hidden = true; if (S.view === "verify") S.view = "out"; }
// verify.py statuses: class of the badge, label (icon + words, never colour alone)
const VSTATE = { ok: ["new", "✓ inserted"], other_text: ["weak", "! other link text"], other_place: ["weak", "! other passage"],
  outside_content: ["weak", "! menu or footer only"], missing: ["orphan", "✗ not linked yet"],
  target_error: ["orphan", "✗ target does not load"], page_error: ["orphan", "✗ page did not load"] };
$("f-lang").innerHTML = '<option value="all">all</option>' + langs.map(l => `<option>${esc(l)}</option>`).join("");
if (langs.length < 2) $("f-lang-wrap").hidden = true;
$("f-kw").innerHTML = '<option value="all">all keywords</option>' + keywords.map(k => `<option>${esc(k)}</option>`).join("");
if (!keywords.length) { $("f-kw-wrap").hidden = true; $("tab-kw").hidden = true; if (S.view === "keywords") S.view = "out"; }

function evaluate() {
  const q = S.q.trim().toLowerCase();
  for (const r of rows) {
    let why = "";
    if (r.sc == null) why = "not_judged";
    else if (r.ca * 100 >= S.can) why = "cannibalisation";
    else if (r.cx * 100 < S.ctx) why = "off_topic";
    else if (r.an * 100 < S.anc) why = "weak_anchor";
    else if (r.va * 100 < S.val) why = "low_value";
    else if (r.si != null && r.si * 100 < S.sim) why = "low_similarity";
    else if (r.sc * 100 < S.score) why = r.v && r.v !== "ok" ? r.v : "low_score";
    else if ((S.type !== "all" && r.ty !== S.type) || (S.lang !== "all" && r.l !== S.lang) ||
             (S.kw !== "all" && r.k !== S.kw) ||
             (q && ![r.s, r.st, r.t, r.tt, r.a, r.c].some(x => String(x).toLowerCase().includes(q)))) why = "filtered";
    r._why = why;
  }
  const passing = rows.filter(r => !r._why).sort((a, b) => b.sc - a.sc || b.cx - a.cx || (b.si ?? 0) - (a.si ?? 0));
  const used = new Set(), perPage = {}, perTarget = {}, maxt = S.maxt >= NO_LIMIT ? Infinity : S.maxt;
  for (const r of passing) {
    const key = r.s + "\u0000" + r.a.toLowerCase();
    if (used.has(key)) { r._why = "duplicate_phrase"; continue; }
    if ((perPage[r.s] || 0) >= S.max) { r._why = "limit"; continue; }
    if (!r.k && (perTarget[r.t] || 0) >= maxt) { r._why = "target_limit"; continue; }  // keyword mode: no limit
    used.add(key); perPage[r.s] = (perPage[r.s] || 0) + 1; perTarget[r.t] = (perTarget[r.t] || 0) + 1;
  }
  return rows.filter(r => !r._why);
}

function bar(label, x, inverse) {
  const v = x == null ? 0 : x, good = inverse ? v < 0.3 : v >= 0.8, mid = inverse ? v < 0.6 : v >= 0.5;
  return `<div class="m">${label} <b>${pct(x)}</b><div class="bar"><i class="${good ? "ok" : mid ? "mid" : "low"}" style="width:${Math.round(v * 100)}%"></i></div></div>`;
}
function highlight(ctx, a) {
  const i = ctx.toLowerCase().indexOf(a.toLowerCase());
  return i < 0 ? esc(ctx) : esc(ctx.slice(0, i)) + "<mark>" + esc(ctx.slice(i, i + a.length)) + "</mark>" + esc(ctx.slice(i + a.length));
}
const html = r => `<a href="${r.t}">${r.a}</a>`;
function item(r, mode) {
  const where = mode === "in"
    ? `<div class="k">On page</div><div><a href="${esc(r.s)}" target="_blank">${esc(r.st || path(r.s))}</a> <span class="url">${esc(path(r.s))}</span></div>`
    : `<div class="k">Link to</div><div><a href="${esc(r.t)}" target="_blank">${esc(r.tt || path(r.t))}</a> <span class="url">${esc(path(r.t))}</span>${r.m ? '<span class="pill">already in menu</span>' : ""}</div>`;
  return `<div class="item"><div class="steps">
    <div class="k">In the ${esc(r.pl || "text")}</div><div class="ctx">${highlight(r.c || "", r.a)}</div>
    <div class="k">Link text</div><div><span class="anchor">${esc(r.a)}</span><span class="pill">${esc(r.ty)}</span>${r.k ? `<span class="pill">keyword: ${esc(r.k)}</span>` : ""}</div>
    ${where}</div>
    <div class="metrics">${bar("score", r.sc)}${bar("context", r.cx)}${bar("anchor", r.an)}${bar("value", r.va)}${bar("cannibal.", r.ca, true)}${r.si != null ? bar("similarity", r.si) : ""}</div>
    <div class="actions"><button class="btn" data-copy="${esc(r.a)}">Copy link text</button>
      <button class="btn" data-copy="${esc(r.t)}">Copy URL</button><button class="btn" data-copy="${esc(html(r))}">Copy HTML</button></div></div>`;
}
function groups(list, key) {
  const m = new Map();
  for (const r of list) { if (!m.has(r[key])) m.set(r[key], []); m.get(r[key]).push(r); }
  const title = ([u, l]) => (key === "s" ? l[0].st : l[0].tt) || path(u);
  const cmp = { count: (a, b) => b[1].length - a[1].length,
    weak: (a, b) => weakness(a[0]) - weakness(b[0]) || b[1].length - a[1].length,
    az: (a, b) => title(a).localeCompare(title(b)) };
  return [...m.entries()].sort(cmp[S.sort] || cmp.count);
}
function health(u) {
  const n = inbound(u), st = linkState(u);
  if (st == null) return "";
  return st === "weak" ? `<span class="badge weak">weak · ${n} link${n > 1 ? "s" : ""} in content today</span>`
    : st === "ok" ? `<span class="count">linked from ${n} pages' content today</span>` : stateBadge(u, true);
}
const LIMIT = 80;
let shown = LIMIT;
function render() {
  const acc = evaluate();
  const pages = new Set(acc.map(r => r.s)), targets = new Set(acc.map(r => r.t));
  const judged = rows.filter(r => r.sc != null).length;
  const orphansFixed = [...targets].filter(u => linkState(u) === "orphan").length;
  $("stats").innerHTML = [["Links to insert", acc.length], ["Pages getting links", pages.size],
    ["Pages linked to", targets.size], ["Orphans fixed", Object.keys(D.pages).length ? orphansFixed : "–"],
    ["Share accepted", judged ? Math.round(100 * acc.length / judged) + "%" : "–"],
    ...(D.verify.length ? [["Inserted (verified)", `${D.verify.filter(v => v.st === "ok").length} / ${D.verify.length}`]] : [])]
    .map(([l, v]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("on", t.dataset.view === S.view));
  const graphView = S.view === "graph";
  $("graph-wrap").hidden = !graphView; $("view").hidden = graphView;
  if (graphView) G.show(acc); else G.hide();
  $("sort").value = S.sort;
  $("sort").hidden = $("toggle-all").hidden = !["out", "in", "pagelist"].includes(S.view);
  $("toggle-all").hidden = !["out", "in"].includes(S.view);
  $("toggle-all").textContent = expandAll ? "Collapse all" : "Expand all";
  const v = $("view");
  if (graphView) return;
  if (S.view === "out" || S.view === "in") {
    const key = S.view === "out" ? "s" : "t";
    const g = groups(acc, key);
    if (!g.length) { v.innerHTML = '<div class="empty">No links pass these filters - lower a threshold on the left.</div>'; return; }
    v.innerHTML = g.slice(0, shown).map(([url, list], n) => {
      const title = key === "s" ? (list[0].st || path(url)) : (list[0].tt || path(url));
      const label = key === "s" ? `links to ${list.length} page${list.length > 1 ? "s" : ""}` : `+${list.length} new link${list.length > 1 ? "s" : ""}`;
      const chips = list.slice(0, 8).map(r => key === "s"
        ? `<span><b>“${esc(r.a)}”</b><span class="arrow">→</span>${esc(r.tt || path(r.t))}</span>`
        : `<span>${esc(r.st || path(r.s))}<span class="arrow">·</span><b>“${esc(r.a)}”</b></span>`).join("") +
        (list.length > 8 ? `<span class="muted">+${list.length - 8} more</span>` : "");
      const open = expandAll || openGroups.has(url) ? " open" : "";
      return `<details class="group" data-url="${esc(url)}"${open}><summary><div class="group-h"><div>
          <h3><span class="chev">▸</span>${esc(title)}</h3><a class="url" href="${esc(url)}" target="_blank">${esc(url)}</a></div>
          <div><span class="badge new">${label}</span> ${key === "t" ? health(url) : ""}</div></div>
        <div class="compact">${chips}</div></summary>${list.map(r => item(r, S.view)).join("")}</details>`;
    }).join("") + (g.length > shown ? `<div class="more"><button class="btn" id="more">Show more (${g.length - shown} left)</button></div>` : "");
  } else if (S.view === "pagelist") {
    const urls = new Set([...Object.keys(D.pages), ...rows.map(r => r.s), ...rows.map(r => r.t)]);
    const outN = {}, inN = {};
    for (const r of acc) { outN[r.s] = (outN[r.s] || 0) + 1; inN[r.t] = (inN[r.t] || 0) + 1; }
    const title = u => (rows.find(r => r.s === u) || {}).st || (rows.find(r => r.t === u) || {}).tt || (D.pages[u] || {}).title || path(u);
    const list = [...urls].map(u => ({ u, t: title(u), today: inbound(u), any: (D.pages[u] || {}).any, out: (D.pages[u] || {}).out,
      nin: inN[u] || 0, nout: outN[u] || 0 }));
    const cmp = { count: (a, b) => b.nin - a.nin || b.nout - a.nout, az: (a, b) => a.t.localeCompare(b.t),
      weak: (a, b) => weakness(a.u) - weakness(b.u) || b.nin - a.nin };
    list.sort(cmp[S.sort] || cmp.count);
    const q = S.q.trim().toLowerCase();
    const shownList = list.filter(x => !q || (x.u + " " + x.t).toLowerCase().includes(q));
    v.innerHTML = (Object.keys(D.pages).length ? "" : '<p class="muted">Links today are unknown - pages.jsonl was not available when the report was built.</p>') +
      `<section class="group"><table class="pl"><tr><th>Page</th><th title="other pages linking to it from their content">Linked from content today</th>
        <th title="other pages linking to it from anywhere, menu and breadcrumbs included">All internal links in</th>
        <th>Links out today</th><th>New links in</th><th>New links out</th><th></th></tr>` +
      shownList.slice(0, shown * 5).map(x => `<tr><td><a href="${esc(x.u)}" target="_blank">${esc(x.t)}</a>${pageKind(x.u)}<div class="url">${esc(path(x.u))}</div></td>
        <td class="n">${x.today ?? "–"} ${stateBadge(x.u)}</td>
        <td class="n">${x.any ?? "–"}</td>
        <td class="n">${x.out ?? "–"}</td>
        <td class="n">${x.nin ? `<span class="plus">+${x.nin}</span>` : "–"}</td><td class="n">${x.nout ? `<span class="plus">+${x.nout}</span>` : "–"}</td>
        <td>${x.nin ? `<button class="linkish" data-goto="in" data-q="${esc(path(x.u))}">sources ›</button>` : ""}
            ${x.nout ? `<button class="linkish" data-goto="out" data-q="${esc(path(x.u))}">targets ›</button>` : ""}
            ${G.has(x.u) ? `<button class="linkish" data-goto="graph" data-u="${esc(x.u)}">graph ›</button>` : ""}</td></tr>`).join("") +
      "</table></section>" + (shownList.length > shown * 5 ? `<div class="more"><button class="btn" id="more">Show more (${shownList.length - shown * 5} left)</button></div>` : "");
  } else if (S.view === "verify") {
    const counts = D.verify.reduce((o, x) => (o[x.st] = (o[x.st] || 0) + 1, o), {});
    const where = D.verify[0].w === "live" ? "on the live site" : D.verify[0].w === "crawl" ? "on the last crawl" : "on " + D.verify[0].w;
    const list = D.verify.filter(x => !S.hidden[x.st] && (!S.q || [x.s, x.t, x.a].some(y => y.toLowerCase().includes(S.q.trim().toLowerCase()))))
      .sort((a, b) => (a.st === "ok") - (b.st === "ok"));
    v.innerHTML = `<p class="muted">${D.verify.length} links checked ${esc(where)}, ${esc(D.verify[0].c.replace("T", " "))}.
        Check again after changes: <code>internal-linking verify --domain ${esc(D.domain)}</code> (add <code>--base-url</code> for a local preview).</p>
      <div class="chips">` + Object.keys(VSTATE).filter(k => counts[k]).map(k =>
        `<span class="chip v ${S.hidden[k] ? "" : "on"}" data-reason="${k}">${esc(VSTATE[k][1])} · ${counts[k]}</span>`).join("") +
      `</div><section class="group">` + (list.length ? list.slice(0, shown * 5).map(x =>
        `<div class="rej"><div>${esc(path(x.s))} · <b>“${esc(x.a)}”</b> → <a href="${esc(x.t)}" target="_blank">${esc(path(x.t))}</a>
          ${x.f && x.st !== "ok" ? `<div class="muted" style="font-size:13px">found: “${esc(x.f)}”</div>` : ""}
          ${x.n ? `<div class="muted" style="font-size:13px">${esc(x.n)}</div>` : ""}</div>
          <div><span class="badge ${(VSTATE[x.st] || ["weak"])[0]}">${esc((VSTATE[x.st] || [0, x.st])[1])}</span></div></div>`).join("")
        : '<div class="empty">Nothing with these filters.</div>') + "</section>" +
      (list.length > shown * 5 ? `<div class="more"><button class="btn" id="more">Show more (${list.length - shown * 5} left)</button></div>` : "");
  } else if (S.view === "keywords") {
    const kws = groups(rows.filter(r => r.k), "k");
    v.innerHTML = `<section class="group"><table class="kw"><tr><th>Keyword</th><th>Target</th><th>Found on</th><th>Links</th><th>Main reject reasons</th></tr>` +
      kws.map(([k, list]) => {
        const a = list.filter(r => !r._why).length;
        const why = Object.entries(list.filter(r => r._why).reduce((o, r) => (o[r._why] = (o[r._why] || 0) + 1, o), {}))
          .sort((x, y) => y[1] - x[1]).slice(0, 3).map(([w, n]) => `${w} ${n}`).join(", ");
        return `<tr><td><b>${esc(k)}</b></td><td><a href="${esc(list[0].t)}" target="_blank">${esc(path(list[0].t))}</a></td>
          <td>${list.length} pages</td><td><b>${a}</b></td><td class="muted">${esc(why)}</td></tr>`;
      }).join("") + "</table></section>";
  } else {
    const rej = rows.filter(r => r._why && !S.hidden[r._why]);
    const counts = rows.filter(r => r._why).reduce((o, r) => (o[r._why] = (o[r._why] || 0) + 1, o), {});
    v.innerHTML = `<div class="chips">` + Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([w, n]) =>
        `<span class="chip ${S.hidden[w] ? "" : "on"}" data-reason="${w}" title="${esc(REASONS[w] || w)}">${esc(w)} · ${n}</span>`).join("") +
      `</div><section class="group">` + (rej.length ? rej.slice(0, shown * 5).map(r =>
        `<div class="rej"><div>${esc(path(r.s))} · <b>“${esc(r.a)}”</b> → <a href="${esc(r.t)}" target="_blank">${esc(path(r.t))}</a>
          <div class="muted" style="font-size:13px">${highlight((r.c || "").slice(0, 220), r.a)}</div></div>
          <div class="reason">${esc(REASONS[r._why] || r._why)}<div class="muted">score ${pct(r.sc)}</div></div></div>`).join("")
        : '<div class="empty">Nothing rejected with these filters.</div>') + "</section>" +
      (rej.length > shown * 5 ? `<div class="more"><button class="btn" id="more">Show more (${rej.length - shown * 5} left)</button></div>` : "");
  }
}
function sync() {
  for (const k of ["score", "ctx", "anc", "val", "can", "sim", "max", "maxt"]) {
    $("f-" + k).value = S[k];
    $("v-" + k).textContent = k === "maxt" ? (S[k] >= NO_LIMIT ? "no limit" : S[k]) : k === "max" ? S[k] : (k === "can" ? "< " : "≥ ") + S[k] + "%";
  }
  $("f-type").value = S.type; $("f-lang").value = S.lang; $("f-kw").value = S.kw; $("f-q").value = S.q;
  try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {}
}
function update() { shown = LIMIT; sync(); render(); }
for (const k of ["score", "ctx", "anc", "val", "can", "sim", "max", "maxt"]) $("f-" + k).addEventListener("input", e => { S[k] = +e.target.value; update(); });
for (const [id, k] of [["f-type", "type"], ["f-lang", "lang"], ["f-kw", "kw"]]) $(id).addEventListener("change", e => { S[k] = e.target.value; update(); });
$("f-q").addEventListener("input", e => { S.q = e.target.value; update(); });
$("reset").addEventListener("click", () => { S = { ...S, ...D.defaults, type:"all", lang:"all", kw:"all", q:"", hidden:{} }; update(); });
document.querySelector(".tabs").addEventListener("click", e => { const t = e.target.closest(".tab"); if (t) { S.view = t.dataset.view; update(); } });
$("view").addEventListener("toggle", e => {
  const d = e.target.closest("details.group");
  if (d) d.open ? openGroups.add(d.dataset.url) : openGroups.delete(d.dataset.url);
}, true);
$("sort").addEventListener("change", e => { S.sort = e.target.value; update(); });
$("toggle-all").addEventListener("click", () => { expandAll = !expandAll; if (!expandAll) openGroups.clear(); render(); });
$("view").addEventListener("click", e => {
  const go = e.target.closest("[data-goto]");
  if (go && go.dataset.goto === "graph") { S.view = "graph"; update(); G.focusUrl(go.dataset.u); window.scrollTo(0, 0); return; }
  if (go) { S.view = go.dataset.goto; S.q = go.dataset.q; expandAll = true; update(); window.scrollTo(0, 0); return; }
  const c = e.target.closest("[data-copy]");
  if (c) { navigator.clipboard?.writeText(c.dataset.copy).then(() => { $("toast").classList.add("show"); setTimeout(() => $("toast").classList.remove("show"), 1200); }); return; }
  const chip = e.target.closest(".chip");
  if (chip) { S.hidden[chip.dataset.reason] = !S.hidden[chip.dataset.reason]; sync(); render(); return; }
  if (e.target.id === "more") { shown += LIMIT; render(); }
});
$("csv").addEventListener("click", () => {
  const acc = evaluate(), cols = ["source_url", "source_title", "placement", "anchor", "anchor_type", "target_url", "target_title", "score", "similarity", "context"];
  const q = x => '"' + String(x ?? "").replace(/"/g, '""') + '"';
  const lines = [cols.join(",")].concat(acc.map(r => [r.s, r.st, r.pl, r.a, r.ty, r.t, r.tt, r.sc, r.si, r.c].map(q).join(",")));
  download("﻿" + lines.join("\n"), `links-${D.domain}-${stamp()}.csv`, "text/csv");
});
function download(text, name, type) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type })); a.download = name; a.click();
}
// the same file as links-for-agent.md from `run` (agent.py), for the current selection
function agentMarkdown(list) {
  const by = new Map(), oneLine = x => String(x ?? "").split(/\s+/).filter(Boolean).join(" ");
  for (const r of list) { if (!by.has(r.s)) by.set(r.s, []); by.get(r.s).push(r); }
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const filters = `thresholds: score ≥ ${S.score}%, context ≥ ${S.ctx}%, anchor ≥ ${S.anc}%, cannibalisation < ${S.can}%, ` +
    `at most ${S.max} per page` + (S.maxt < NO_LIMIT ? `, at most ${S.maxt} to one page` : "") + (S.val ? `, value ≥ ${S.val}%` : "") + (S.sim ? `, page similarity ≥ ${S.sim}%` : "");
  const tpath = u => { try { const x = new URL(u); return (x.pathname || "/") + x.search; } catch (e) { return u; } };
  const lines = [`# Internal links to insert - ${D.domain}`, "",
    `${plural(list.length, "link")} on ${plural(by.size, "page")} · generated ${stamp().replace(/-(\d\d)(\d\d)$/, " $1:$2")} ` +
      `from the report of the run ${timeOf(D.run_at || D.built_at)} by internal-linking ${D.version} · ${filters}`,
    "", D.agent, "", "## Links", ""];
  for (const [s, rs] of by) {
    lines.push(`### ${tpath(s)} - ${rs[0].st || ""}`, "", `Page: ${s}`, "");
    rs.forEach((r, i) => lines.push(`${i + 1}. Link text: \`${r.a}\` → \`${tpath(r.t)}\` - ${r.tt || ""}`, `   > ${oneLine(r.c)}`, ""));
  }
  const cell = x => { x = oneLine(x); return /[",\r\n]/.test(x) ? '"' + x.replace(/"/g, '""') + '"' : x; };
  lines.push("## For `internal-linking verify` - do not edit", "", "```csv", "source_url,anchor,target_url,context",
    ...list.map(r => [r.s, r.a, r.t, r.c].map(cell).join(",")), "```", "");
  return lines.join("\n");
}
$("md").addEventListener("click", () => {
  const acc = [...evaluate()].sort((a, b) => a.s.localeCompare(b.s) || b.sc - a.sc);
  download(agentMarkdown(acc), `links-for-agent-${D.domain}-${stamp()}.md`, "text/markdown");
});
// ---- Graph view: the whole site with force-graph (inlined above) - pages, links today, new links, similarity ----
const G = (() => {
  const g = D.graph || { nodes: [], links: [], near: [] };
  const N = g.nodes.length;
  const hasMap = g.nodes.some(n => n.x != null);
  const hasSim = g.links.some(l => l[2] != null) || g.near.some(l => l.length);
  const nodes = g.nodes.map((n, i) => ({ id: i, u: n.u, t: n.t || path(n.u), hx: n.x, hy: n.y, in0: 0, out0: 0, nin: 0, nout: 0 }));
  const byUrl = new Map(nodes.map(n => [n.u, n]));
  for (const [i, j] of g.links) { nodes[i].out0++; nodes[j].in0++; }
  const SCALE = Math.max(260, Math.sqrt(N) * 38);  // half-width of the topic map in graph units
  for (const n of nodes) if (n.hx != null) { n.x = n.hx * SCALE; n.y = n.hy * SCALE; }
  const GKEY = "il-graph:" + D.domain;
  const st = { arrange: hasMap ? "topics" : "links", today: true, fresh: true, sim: false, set: N > 1500 ? "new" : "all" };
  try { Object.assign(st, JSON.parse(localStorage.getItem(GKEY) || "{}")); } catch (e) {}
  if (!hasMap) st.arrange = "links";
  let fg = null, focus = null, hover = null, rel = new Map(), labelled = new Set(), landmarks = new Set();
  let R = 160, edges = [], sig = null, centerForce = null, fitted = false;
  const C = {};
  const save = () => { try { localStorage.setItem(GKEY, JSON.stringify(st)); } catch (e) {} };
  const end = x => (typeof x === "object" ? x : nodes[x]);
  const touches = (l, n) => !!n && (end(l.source) === n || end(l.target) === n);
  const radius = n => Math.min(18, 3 + 1.7 * Math.sqrt(n.in0 + n.nin));
  const fill = n => { const st = linkState(n.u); return st === "orphan" ? C.bad : st === "none" || st === "weak" ? C.warn : C.node; };
  // distance from the page in focus: the more similar, the closer
  const ringR = s => R * (0.3 + 0.7 * Math.min(1, Math.max(0, (1 - (s ?? 0.55)) / 0.8)));
  const trunc = (s, k) => (s.length > k ? s.slice(0, k - 1) + "…" : s);
  const median = a => { if (!a.length) return null; const b = [...a].sort((x, y) => x - y), m = b.length >> 1; return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2; };
  const narrow = () => matchMedia("(max-width: 860px)").matches;

  function colors() {
    const cs = getComputedStyle(document.documentElement), v = k => cs.getPropertyValue(k).trim();
    Object.assign(C, { node: v("--g-node"), today: v("--g-today"), fresh: v("--g-new"), sim: v("--g-sim"), fade: v("--g-fade"),
      bad: v("--bad"), warn: v("--warn"), ink: v("--ink"), muted: v("--muted"), surface: v("--panel"), line: v("--line") });
  }

  // graph data: new links (current filters), links in content today, closest unlinked pairs
  function data() {
    let ids = null;
    if (st.set === "new") {
      ids = new Set();
      for (const [a, b] of edges) { ids.add(a.id); ids.add(b.id); }
      if (focus) { ids.add(focus.id); for (const id of rel.keys()) ids.add(id); }
    }
    const ok = i => !ids || ids.has(i), links = [], pairs = new Set();
    for (const [a, b, r] of edges) if (ok(a.id) && ok(b.id)) {
      links.push({ source: a.id, target: b.id, kind: "new", sim: r.si, r });
      pairs.add(a.id + ">" + b.id); pairs.add(b.id + ">" + a.id);
    }
    for (const [i, j, s] of g.links) if (ok(i) && ok(j)) links.push({ source: i, target: j, kind: "today", sim: s });
    const seen = new Set();
    g.near.forEach((list, i) => {
      if (!ok(i)) return;
      for (const [j, s] of list) {
        const key = Math.min(i, j) + "-" + Math.max(i, j);
        if (ok(j) && !seen.has(key) && !pairs.has(i + ">" + j)) { seen.add(key); links.push({ source: i, target: j, kind: "sim", sim: s }); }
      }
    });
    const deg = {};
    for (const l of links) if (l.kind !== "sim") { deg[l.source] = (deg[l.source] || 0) + 1; deg[l.target] = (deg[l.target] || 0) + 1; }
    for (const l of links) l.deg = Math.min(deg[l.source] || 1, deg[l.target] || 1);
    return { nodes: nodes.filter(n => ok(n.id)), links };
  }

  function linkVisible(l) {
    const shown = l.kind === "today" ? st.today : l.kind === "new" ? st.fresh : false;
    if (focus) return touches(l, focus) ? shown || l.kind === "sim" : shown;
    return shown || (l.kind === "sim" && (st.sim || touches(l, hover)));
  }
  function linkColor(l) {
    const on = touches(l, focus || hover);
    if (focus && !on) return C.fade;
    return l.kind === "new" ? C.fresh : l.kind === "sim" ? C.sim : on ? C.muted : C.today;
  }
  function linkWidth(l) {
    const on = touches(l, focus || hover);
    return l.kind === "new" ? (on ? 2.6 : 1.5) : l.kind === "sim" ? 1.5 : on ? 1.4 : 0.7;
  }
  const arrow = l => (l.kind === "new" ? 5 : l.kind === "today" && touches(l, focus) ? 4 : 0);
  const particles = l => (l.kind === "new" && touches(l, focus) ? 2 : 0);
  const refresh = () => { if (fg) fg.linkWidth(linkWidth); };  // any prop change repaints the canvas

  function drawNode(n, ctx, k) {
    const r = radius(n), dim = focus && n !== focus && !rel.has(n.id);
    ctx.globalAlpha = dim ? 0.13 : 1;
    ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, 2 * Math.PI);
    ctx.fillStyle = fill(n); ctx.fill();
    ctx.lineWidth = 2 / k; ctx.strokeStyle = C.surface; ctx.stroke();  // 2px surface ring
    if (n === focus || n === hover) {
      ctx.beginPath(); ctx.arc(n.x, n.y, r + 3 / k, 0, 2 * Math.PI);
      ctx.lineWidth = 2 / k; ctx.strokeStyle = C.fresh; ctx.stroke();
    }
    if (!dim && (n === focus || n === hover || (focus ? labelled.has(n.id) : landmarks.has(n.id) || k * r > 14))) {
      const text = trunc(n.t, n === focus ? 60 : 38), y = n.y + r + 3 / k;
      ctx.font = `${n === focus ? 700 : 500} ${(n === focus ? 13 : 11.5) / k}px Manrope, system-ui, sans-serif`;
      ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.lineJoin = "round";
      ctx.lineWidth = 3 / k; ctx.strokeStyle = C.surface; ctx.strokeText(text, n.x, y);
      ctx.fillStyle = C.ink; ctx.fillText(text, n.x, y);
    }
    ctx.globalAlpha = 1;
  }
  // similarity scale around the page in focus
  function drawRings(ctx, k) {
    if (!focus || !hasSim) return;
    ctx.save();
    ctx.font = `500 ${11 / k}px Manrope, system-ui, sans-serif`; ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.lineJoin = "round";
    for (const s of [0.9, 0.75, 0.6, 0.45]) {
      const r = ringR(s), a = -Math.PI / 4, x = focus.x + r * Math.cos(a) + 3 / k, y = focus.y + r * Math.sin(a);
      ctx.beginPath(); ctx.arc(focus.x, focus.y, r, 0, 2 * Math.PI);
      ctx.lineWidth = 1 / k; ctx.strokeStyle = C.line; ctx.stroke();
      ctx.lineWidth = 3 / k; ctx.strokeStyle = C.surface; ctx.strokeText(Math.round(s * 100) + "%", x, y);
      ctx.fillStyle = C.muted; ctx.fillText(Math.round(s * 100) + "%", x, y);
    }
    ctx.restore();
  }
  function tooltip(n) {
    const e = focus && rel.get(n.id);
    const st = STATE[linkState(n.u)];
    return `<b>${esc(n.t)}</b><span class="url">${esc(path(n.u))}</span>${st ? esc(st[1]) + "<br>" : ""}links in content today ${n.in0} · out ${n.out0}` +
      (n.nin || n.nout ? `<br>new links: +${n.nin} in · +${n.nout} out` : "") +
      (e && e.sim != null ? `<br><b>${pct(e.sim)}</b> similar to “${esc(trunc(focus.t, 32))}”` : "");
  }

  // forces: "by topic" pulls every page to its place on the topic map (embeddings), "by links"
  // lets linked pages pull together; in focus the page's links gather on rings by similarity and
  // every other page is pushed out of the circle
  function homeForce() {
    let ns = [];
    const f = alpha => {
      if (st.arrange !== "topics" || focus) return;
      for (const n of ns) if (n.hx != null) { n.vx += (n.hx * SCALE - n.x) * 0.12 * alpha; n.vy += (n.hy * SCALE - n.y) * 0.12 * alpha; }
    };
    f.initialize = x => { ns = x; };
    return f;
  }
  function focusForce() {
    let ns = [];
    const f = alpha => {
      if (!focus) return;
      for (const n of ns) {
        if (n === focus) continue;
        const e = rel.get(n.id);
        if (e) {  // its own spot: the ring of its similarity, at its share of the circle
          const r = ringR(e.sim);
          n.vx += (focus.x + r * Math.cos(e.angle) - n.x) * 0.3 * alpha;
          n.vy += (focus.y + r * Math.sin(e.angle) - n.y) * 0.3 * alpha;
          continue;
        }
        const dx = n.x - focus.x, dy = n.y - focus.y, d = Math.hypot(dx, dy) || 1;
        if (d < R * 1.5) { const k = (R * 1.5 - d) / d * 0.35 * alpha; n.vx += dx * k; n.vy += dy * k; }
      }
    };
    f.initialize = x => { ns = x; };
    return f;
  }
  function forces() {
    const topics = st.arrange === "topics";
    fg.d3Force("charge").strength(topics ? -5 : -50).distanceMax(topics ? 50 : 600);
    fg.d3Force("link").distance(l => 25 + 160 * (1 - (l.sim ?? 0.5)))
      .strength(l => (topics || l.kind === "sim" || touches(l, focus) ? 0 : 0.7 / l.deg));
    fg.d3ReheatSimulation();
  }

  function relations(n) {
    const m = new Map();
    const add = (id, sim, kind, r) => {
      if (id === n.id) return;
      const e = m.get(id) || { sim: null, kinds: [], rows: [] };
      if (e.sim == null && sim != null) e.sim = sim;
      e.kinds.push(kind);
      if (r) e.rows.push(r);
      m.set(id, e);
    };
    for (const [i, j, s] of g.links) { if (i === n.id) add(j, s, "out0"); else if (j === n.id) add(i, s, "in0"); }
    for (const [a, b, r] of edges) { if (a === n) add(b.id, r.si, "nout", r); else if (b === n) add(a.id, r.si, "nin", r); }
    for (const [j, s] of g.near[n.id] || []) if (!m.has(j)) add(j, s, "near");
    return m;
  }
  // the related pages share the circle evenly, in the order they already sit around the page
  function spread() {
    const fx = focus.x ?? 0, fy = focus.y ?? 0, angle = id => Math.atan2((nodes[id].y ?? fy + 1) - fy, (nodes[id].x ?? fx) - fx);
    const ids = [...rel.keys()].sort((a, b) => angle(a) - angle(b)), start = ids.length ? angle(ids[0]) : 0;
    ids.forEach((id, i) => { rel.get(id).angle = start + 2 * Math.PI * i / ids.length; });
  }
  function frame(n, r, ms) {
    const el = $("graph"), w = el.clientWidth, h = el.clientHeight;
    const pw = focus && !narrow() ? 352 : 0, ph = focus && narrow() ? h * 0.46 : 0;
    const k = Math.min((w - pw) / (2 * r), (h - ph) / (2 * r), 8);
    fg.centerAt(n.x + pw / 2 / k, n.y + ph / 2 / k, ms);
    fg.zoom(k, ms);
  }
  function focusOn(n) {
    if (!fg || !n) return;
    if (focus && focus !== n) focus.fx = focus.fy = undefined;
    focus = n; rel = relations(n);
    spread();
    R = Math.max(150, 48 * Math.sqrt(rel.size + 1));
    labelled = new Set([...rel.entries()].sort((a, b) => (b[1].sim ?? 0) - (a[1].sim ?? 0)).slice(0, 24).map(([id]) => id));
    for (const [id, e] of rel) if (e.rows.length) labelled.add(id);
    if (st.set !== "all") fg.graphData(data());
    if (n.x == null) { n.x = 0; n.y = 0; }
    n.fx = n.x; n.fy = n.y;
    if (fg.d3Force("center")) { centerForce = fg.d3Force("center"); fg.d3Force("center", null); }
    forces();
    fg.linkDirectionalParticles(particles);
    frame(n, R * 1.3, 800);
    panel();
    $("g-hint").hidden = true;
  }
  function unfocus() {
    if (!focus || !fg) return;
    focus.fx = focus.fy = undefined;
    focus = null; rel = new Map(); labelled = new Set();
    if (centerForce) fg.d3Force("center", centerForce);
    if (st.set !== "all") fg.graphData(data());
    forces();
    fg.linkDirectionalParticles(particles);
    panel();
    setTimeout(() => { if (fg && !focus) fg.zoomToFit(700, 50); }, 700);
  }

  const KIND = { nout: ["New links out", "new"], nin: ["New links in", "new"], out0: ["Links out today", "today"],
    in0: ["Links in today", "today"], near: ["Closest pages without a link", "sim"] };
  const COLOR = { new: "var(--g-new)", today: "var(--g-node)", sim: "var(--g-sim)" };
  const key = kind => `<span class="key${kind === "sim" ? " dash" : ""}" style="border-color:${COLOR[kind]}"></span>`;
  function panel() {
    const el = $("focus");
    if (!focus) { el.hidden = true; el.innerHTML = ""; return; }
    const n = focus;
    const badge = stateBadge(n.u, true);
    const secs = Object.entries(KIND).map(([k, [label, kind]]) => {
      const list = [...rel.entries()].filter(([, e]) => e.kinds.includes(k)).sort((a, b) => (b[1].sim ?? -1) - (a[1].sim ?? -1));
      if (!list.length) return "";
      return `<div class="fsec"><h4>${key(kind)}${esc(label)} · ${list.length}</h4>` + list.slice(0, 40).map(([id, e]) => {
        const anchors = e.rows.filter(r => (k === "nout" ? r.s === n.u : k === "nin" && r.t === n.u)).map(r => "“" + r.a + "”").join(", ");
        return `<button class="frow" data-node="${id}"><span class="fsim">${e.sim == null ? "–" : pct(e.sim)}` +
          `<span class="bar"><i style="width:${Math.round((e.sim ?? 0) * 100)}%;background:${COLOR[kind]}"></i></span></span>` +
          `<span><span class="ft">${esc(nodes[id].t)}</span>${anchors ? `<span class="fa">${esc(anchors)}</span>` : ""}</span></button>`;
      }).join("") + (list.length > 40 ? `<p class="muted" style="font-size:12px;margin:6px 0 0">+${list.length - 40} more</p>` : "") + "</div>";
    }).join("");
    el.innerHTML = `<button class="btn x" data-unfocus title="Back to the whole site (Esc)">✕</button>
      <h3>${esc(n.t)}</h3><a class="url" href="${esc(n.u)}" target="_blank">${esc(path(n.u))}</a> ${badge}
      <div class="fstats"><div><b>${n.in0}</b>links in today</div><div><b>${n.out0}</b>links out today</div>
        <div><b class="plus">+${n.nin}</b>new links in</div><div><b class="plus">+${n.nout}</b>new links out</div></div>` +
      (secs || '<p class="muted">No links to or from this page yet.</p>') +
      (hasSim ? "" : '<p class="muted" style="font-size:12px">Similarity and the closest pages need embeddings - run <code>internal-linking embed</code>.</p>');
    el.hidden = false; el.scrollTop = 0;
  }

  // similarity of the links today vs the new links: two histograms on one scale, a table, stat tiles
  function histogram(title, values, color, lo20, ymax) {
    const nb = 20 - lo20, W = 440, H = 170, L = 38, Rp = 8, T = 10, B = 24, band = (W - L - Rp) / nb, bw = Math.min(24, band - 2);
    const counts = Array(nb).fill(0), total = values.length || 1, y = v => T + (H - T - B) * (1 - v / ymax), y0 = y(0);
    for (const s of values) counts[Math.min(nb - 1, Math.max(0, Math.floor(s * 20) - lo20))]++;
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}: similarity histogram">`;
    for (const f of [0, 0.5, 1]) {
      const yy = y(ymax * f).toFixed(1);
      svg += `<line class="grid" x1="${L}" x2="${W - Rp}" y1="${yy}" y2="${yy}"/><text x="${L - 6}" y="${+yy + 4}" text-anchor="end">${Math.round(ymax * f * 100)}%</text>`;
    }
    for (let i = 0; i <= nb; i++) if ((lo20 + i) % 2 === 0) svg += `<text x="${(L + i * band).toFixed(1)}" y="${H - 5}" text-anchor="middle">${(lo20 + i) * 5}%</text>`;
    counts.forEach((c, i) => {
      const share = c / total, x = L + i * band + (band - bw) / 2, top = y(share), r = Math.min(4, bw / 2, y0 - top);
      svg += `<rect class="hit" x="${(L + i * band).toFixed(1)}" y="${T}" width="${band.toFixed(1)}" height="${H - T - B}" tabindex="0" ` +
        `data-v="${c} link${c === 1 ? "" : "s"} · ${Math.round(share * 100)}%" data-l="${esc(title)} · similarity ${(lo20 + i) * 5}–${(lo20 + i + 1) * 5}%"/>`;
      if (c) svg += `<path d="M${x.toFixed(1)},${y0.toFixed(1)}V${(top + r).toFixed(1)}Q${x.toFixed(1)},${top.toFixed(1)} ${(x + r).toFixed(1)},${top.toFixed(1)}` +
        `H${(x + bw - r).toFixed(1)}Q${(x + bw).toFixed(1)},${top.toFixed(1)} ${(x + bw).toFixed(1)},${(top + r).toFixed(1)}V${y0.toFixed(1)}Z" fill="${color}" pointer-events="none"/>`;
    });
    const md = median(values);
    return { counts, html: `<section class="group chart"><h4>${esc(title)}</h4><p>${values.length} link${values.length === 1 ? "" : "s"}` +
      (md != null ? ` · median similarity ${pct(md)}` : "") + " · share of links per 5% of similarity</p>" + svg + "</svg></section>" };
  }
  function summary() {
    const today = g.links.map(l => l[2]).filter(s => s != null);
    const fresh = edges.map(e => e[2].si).filter(s => s != null);
    const typical = median(today), newPairs = new Set(edges.map(([a, b]) => Math.min(a.id, b.id) + "-" + Math.max(a.id, b.id)));
    const close = new Set();
    if (typical != null) g.near.forEach((list, i) => { for (const [j, s] of list) {
      const k = Math.min(i, j) + "-" + Math.max(i, j);
      if (s >= typical && !newPairs.has(k)) close.add(k);
    } });
    const md = median(fresh);
    $("g-cards").innerHTML = [
      [g.links.length, "links in content today" + (typical != null ? ` · median similarity ${pct(typical)}` : "")],
      [edges.length, "new links selected" + (md != null ? ` · median similarity ${pct(md)}` : "")],
      [`${nodes.filter(n => linkState(n.u) === "orphan").length} → ${nodes.filter(n => linkState(n.u) === "orphan" && !n.nin).length}`,
        "orphan pages (no internal links) today → after the changes"],
      [`${nodes.filter(n => n.in0 === 0).length} → ${nodes.filter(n => n.in0 === 0 && !n.nin).length}`,
        "pages without links in content today → after the changes"],
      ...(typical != null ? [[close.size, "unlinked page pairs at least as similar as a typical link today"]] : []),
    ].map(([v, l]) => `<div class="stat"><b>${v}</b><span>${esc(l)}</span></div>`).join("");
    if (!today.length && !fresh.length) {
      $("g-charts").innerHTML = hasSim ? "" : `<p class="muted">The similarity charts need embeddings: <code>internal-linking embed --domain ${esc(D.domain)}</code>.</p>`;
      return;
    }
    const lo20 = Math.min(18, Math.max(0, Math.floor(Math.min(...today, ...fresh) * 20)));
    const share = vs => { const c = Array(20 - lo20).fill(0); for (const s of vs) c[Math.min(19 - lo20, Math.max(0, Math.floor(s * 20) - lo20))]++; return Math.max(...c) / (vs.length || 1); };
    const ymax = Math.max(0.05, Math.ceil(Math.max(share(today), share(fresh)) * 20) / 20);
    const a = histogram("Links in content today", today, "var(--g-node)", lo20, ymax);
    const b = histogram("New links", fresh, "var(--g-new)", lo20, ymax);
    $("g-charts").innerHTML = a.html + b.html + `<details class="tbl"><summary>Show as a table</summary><table class="kw">` +
      `<tr><th>Similarity</th><th>Links today</th><th>New links</th></tr>` +
      a.counts.map((c, i) => `<tr><td>${(lo20 + i) * 5}–${(lo20 + i + 1) * 5}%</td><td>${c}</td><td>${b.counts[i]}</td></tr>`).join("") + "</table></details>";
  }

  function controls() {
    document.querySelectorAll("[data-arrange]").forEach(b => {
      b.classList.toggle("on", b.dataset.arrange === st.arrange);
      if (b.dataset.arrange === "topics" && !hasMap) { b.disabled = true; b.title = "needs embeddings - run internal-linking embed"; }
    });
    $("g-today").checked = st.today; $("g-new").checked = st.fresh; $("g-sim").checked = st.sim;
    $("g-sim-wrap").hidden = !hasSim; $("g-set").value = st.set;
  }
  function legend() {
    $("g-legend").innerHTML = `<span><i class="sw" style="background:var(--bad)"></i>orphan · no internal links</span>` +
      `<span><i class="sw" style="background:var(--warn)"></i>0-2 links in content (menu only, weak)</span>` +
      `<span><i class="sw" style="background:var(--g-node)"></i>3+ links in content</span><span>size = links in after the changes</span>` +
      `<span><i class="key" style="border-color:var(--g-node)"></i>link today</span>` +
      `<span><i class="key" style="border-color:var(--g-new)"></i>new link</span>` +
      (hasSim ? `<span><i class="key dash" style="border-color:var(--g-sim)"></i>similar, not linked</span>` +
        `<span>in focus: rings = similarity to the page</span>` : "");
  }
  function resize() {
    if (!fg || $("graph-wrap").hidden) return;
    const el = $("graph");
    fg.width(el.clientWidth).height(el.clientHeight);
  }
  function fitSoon(ms) { setTimeout(() => { if (fg && !focus) fg.zoomToFit(600, 50); }, ms); }

  function init() {
    colors();
    const el = $("graph");
    fg = ForceGraph()(el)
      .backgroundColor("rgba(0,0,0,0)")
      .nodeId("id").nodeRelSize(1).nodeVal(n => radius(n) ** 2)
      .nodeLabel(tooltip).nodeCanvasObject(drawNode)
      .nodePointerAreaPaint((n, color, ctx, k) => {  // hit area bigger than the circle
        ctx.fillStyle = color; ctx.beginPath(); ctx.arc(n.x, n.y, Math.max(radius(n) + 2 / k, 12 / k), 0, 2 * Math.PI); ctx.fill();
      })
      .linkVisibility(linkVisible).linkColor(linkColor).linkWidth(linkWidth)
      .linkLineDash(l => (l.kind === "sim" ? [4, 3] : null))
      .linkDirectionalArrowLength(arrow).linkDirectionalArrowRelPos(1)
      .linkDirectionalParticles(particles).linkDirectionalParticleWidth(3).linkDirectionalParticleSpeed(0.006)
      .linkDirectionalParticleColor(() => C.fresh)
      .onNodeHover(n => { hover = n || null; el.style.cursor = n ? "pointer" : ""; refresh(); })
      .onNodeClick(n => (n === focus ? unfocus() : focusOn(n)))
      .onBackgroundClick(unfocus)
      .onRenderFramePre(drawRings)
      .onEngineStop(() => { if (!fitted && !focus) { fitted = true; fg.zoomToFit(500, 50); } })
      .warmupTicks(st.arrange === "links" && N < 1500 ? 80 : 0)
      .cooldownTime(12000).d3VelocityDecay(0.35).minZoom(0.03).maxZoom(14);
    fg.d3Force("home", homeForce());
    fg.d3Force("focus", focusForce());
    window.addEventListener("resize", resize);
    $("g-titles").innerHTML = nodes.map(n => `<option value="${esc(n.t)}">`).join("");
    legend();
    fitSoon(250);
  }

  document.querySelector(".gbar").addEventListener("click", e => {
    const b = e.target.closest("[data-arrange]");
    if (!b || b.disabled || !fg) return;
    st.arrange = b.dataset.arrange; save(); controls(); forces(); fitSoon(1500);
  });
  for (const [id, k] of [["g-today", "today"], ["g-new", "fresh"], ["g-sim", "sim"]])
    $(id).addEventListener("change", e => { st[k] = e.target.checked; save(); refresh(); });
  $("g-set").addEventListener("change", e => { st.set = e.target.value; save(); if (fg) { fg.graphData(data()); forces(); fitSoon(900); } });
  $("g-fit").addEventListener("click", () => { if (fg) focus ? frame(focus, R * 1.3, 500) : fg.zoomToFit(500, 50); });
  $("g-find").addEventListener("change", e => {
    const q = e.target.value.trim().toLowerCase();
    const n = q && (nodes.find(n => n.t.toLowerCase() === q) || nodes.find(n => (n.t + " " + n.u).toLowerCase().includes(q)));
    if (n) focusOn(n);
  });
  $("focus").addEventListener("click", e => {
    const r = e.target.closest("[data-node]");
    if (r) focusOn(nodes[+r.dataset.node]); else if (e.target.closest("[data-unfocus]")) unfocus();
  });
  const tip = $("tip");
  const showTip = (h, x, y) => {
    const b = document.createElement("b"), s = document.createElement("span");
    b.textContent = h.dataset.v; s.textContent = h.dataset.l;
    tip.replaceChildren(b, s); tip.style.display = "block";
    tip.style.left = Math.max(8, Math.min(x + 14, innerWidth - tip.offsetWidth - 8)) + "px";
    tip.style.top = Math.min(y + 14, innerHeight - tip.offsetHeight - 8) + "px";
  };
  $("g-charts").addEventListener("pointermove", e => { const h = e.target.closest(".hit"); h ? showTip(h, e.clientX, e.clientY) : (tip.style.display = "none"); });
  $("g-charts").addEventListener("pointerleave", () => { tip.style.display = "none"; });
  $("g-charts").addEventListener("focusin", e => { const h = e.target.closest(".hit"); if (h) { const b = h.getBoundingClientRect(); showTip(h, b.left, b.top); } });
  $("g-charts").addEventListener("focusout", () => { tip.style.display = "none"; });

  function show(acc) {
    if (!N) return;
    const first = !fg;
    if (first) init(); else fg.resumeAnimation();
    controls();
    resize();
    const list = acc.map(r => [byUrl.get(r.s), byUrl.get(r.t), r]).filter(([a, b]) => a && b);
    const s = list.map(([a, b]) => a.id + ">" + b.id).join(",");
    if (s === sig) return;  // the filters changed, but not the set of new links
    sig = s; edges = list;
    for (const n of nodes) { n.nin = 0; n.nout = 0; }
    for (const [a, b] of edges) { a.nout++; b.nin++; }
    landmarks = new Set([...nodes].sort((a, b) => (b.in0 + b.nin) - (a.in0 + a.nin)).slice(0, 12).map(n => n.id));
    if (focus) { rel = relations(focus); spread(); }
    fg.graphData(data());
    forces();
    summary();
    panel();
  }
  return {
    show, unfocus,
    hide: () => { if (fg) fg.pauseAnimation(); },
    has: u => byUrl.has(u),
    focusUrl: u => { const n = byUrl.get(u); if (n) setTimeout(() => focusOn(n), 60); },
    recolor: () => { if (fg) { colors(); refresh(); } },
  };
})();

function theme(t) {
  if (t) { document.documentElement.dataset.theme = t; try { localStorage.setItem("il-theme", t); } catch (e) {} }
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.querySelectorAll("[data-theme-set]").forEach(b => b.classList.toggle("on", b.dataset.themeSet === cur));
  G.recolor();
}
document.querySelector(".theme").addEventListener("click", e => { const b = e.target.closest("[data-theme-set]"); if (b) theme(b.dataset.themeSet); });
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => theme());
document.addEventListener("keydown", e => { if (e.key === "Escape" && S.view === "graph") G.unfocus(); });
theme();
update();
</script></body></html>"""
