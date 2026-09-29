"""Interactive HTML report - one self-contained, CMS-agnostic file.

Every judged suggestion is embedded as data; the page applies the thresholds in the browser, so
the filters (score, context, anchor, value, cannibalisation in %, links per page, match type,
search) change the selection live. It opens with the thresholds used by the run, so its default
selection matches links.csv. `internal-linking report` rebuilds it from saved results.
"""
import json
from datetime import date
from pathlib import Path

from .common import data_dir, read_csv
from .config import load_config

NUMERIC = {"score": "sc", "jev_context": "cx", "jev_anchor": "an", "jev_value": "va", "jev_cannibalisation": "ca"}


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


def write_report(path: Path, domain: str, rows: list[dict], cfg: dict, max_links: int,
                 stats: dict | None = None) -> None:
    payload = {
        "domain": domain,
        "date": date.today().isoformat(),
        "defaults": {"score": round(cfg["score_min"] * 100), "ctx": round(cfg["context_min"] * 100),
                     "anc": round(cfg["anchor_min"] * 100), "val": 0,
                     "can": round(cfg["cannibalisation_max"] * 100), "max": max_links},
        "stats": stats or {},
        "rows": [_compact(r) for r in rows],
    }
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    path.write_text(TEMPLATE.replace("__DATA__", data), encoding="utf-8")


def rebuild(args) -> None:
    """Rebuilds report.html from data/<domain>/recommendations.csv - no crawl, no Jev calls."""
    cfg = load_config(args.config)
    for domain in args.domain:
        ddir = data_dir(domain)
        src = ddir / "recommendations.csv"
        if not src.exists():
            print(f"! {domain}: no {src} - run `internal-linking run --domain {domain}` first")
            continue
        rows = read_csv(src)
        write_report(ddir / "report.html", domain, rows, cfg, args.max_links or cfg["max_links_per_page"])
        print(f"{domain}: {len(rows)} suggestions → {ddir / 'report.html'}")


def add_parser(sub) -> None:
    p = sub.add_parser("report", help="rebuild report.html from saved results (no crawl, no Jev calls)")
    p.add_argument("--domain", action="append", required=True, help="domain (repeatable)")
    p.add_argument("--max-links", type=int, help="default links-per-page filter (default from config)")
    p.set_defaults(func=rebuild)


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Internal links report</title>
<style>
:root { --bg:#f6f5f1; --panel:#ffffff; --ink:#1b1b1d; --muted:#6a6a70; --line:#e3e1db; --soft:#f0eee8;
  --accent:#2e63d6; --accent-soft:#e6edfb; --mark:#ffe98a; --good:#1f8a4c; --warn:#b7791f; --bad:#c2412d;
  --shadow:0 1px 2px rgba(0,0,0,.05); }
@media (prefers-color-scheme: dark) { :root { --bg:#141416; --panel:#1d1d20; --ink:#ececee; --muted:#9a9aa1;
  --line:#313136; --soft:#26262a; --accent:#7ea2ff; --accent-soft:#232c44; --mark:#5b4f1c; --good:#46b875;
  --warn:#d9a441; --bad:#ec6d58; --shadow:none; } }
* { box-sizing:border-box } html,body { margin:0 }
body { background:var(--bg); color:var(--ink); font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif }
a { color:var(--accent); text-decoration:none } a:hover { text-decoration:underline }
header { padding:22px 20px 8px; max-width:1320px; margin:0 auto }
h1 { font-size:22px; margin:0 } .sub { color:var(--muted); margin:2px 0 0; font-size:14px }
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
.btn.primary { background:var(--accent); color:#fff; border-color:var(--accent) }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin-bottom:14px }
.stat { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:10px 14px; box-shadow:var(--shadow) }
.stat b { display:block; font-size:22px; font-variant-numeric:tabular-nums } .stat span { color:var(--muted); font-size:13px }
.tabs { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:14px; align-items:center }
.tab { font:inherit; font-size:14px; border:1px solid var(--line); background:var(--panel); color:var(--ink);
  padding:7px 12px; border-radius:999px; cursor:pointer } .tab.on { background:var(--ink); color:var(--bg); border-color:var(--ink) }
.tabs .right { margin-left:auto; display:flex; gap:6px }
.group { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:16px 18px;
  margin-bottom:14px; box-shadow:var(--shadow) }
.group-h { display:flex; justify-content:space-between; gap:12px; align-items:baseline; flex-wrap:wrap }
.group-h h3 { font-size:16px; margin:0 } .count { color:var(--muted); font-size:13px; white-space:nowrap }
.url { font:12px ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); word-break:break-all }
.item { border-top:1px solid var(--line); margin-top:14px; padding-top:14px }
.steps { display:grid; grid-template-columns:auto 1fr; gap:6px 12px; align-items:start }
.k { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); padding-top:3px; white-space:nowrap }
.ctx { background:var(--soft); border-radius:10px; padding:10px 12px }
mark { background:var(--mark); color:inherit; border-radius:3px; padding:0 2px; font-weight:600 }
.anchor { font-weight:700 } .pill { display:inline-block; font-size:11px; border:1px solid var(--line); border-radius:999px;
  padding:1px 8px; color:var(--muted); margin-left:6px; vertical-align:1px }
.actions { display:flex; gap:6px; flex-wrap:wrap; margin-top:10px }
.metrics { display:flex; gap:14px; flex-wrap:wrap; margin-top:10px }
.m { font-size:12px; color:var(--muted); min-width:92px } .m b { color:var(--ink); font-variant-numeric:tabular-nums }
.bar { height:5px; background:var(--line); border-radius:3px; overflow:hidden; margin-top:3px }
.bar i { display:block; height:100% } .ok { background:var(--good) } .mid { background:var(--warn) } .low { background:var(--bad) }
.rej { display:grid; grid-template-columns:1fr auto; gap:4px 12px; padding:9px 0; border-top:1px solid var(--line); font-size:14px }
.reason { color:var(--bad); font-size:13px; white-space:nowrap } .muted { color:var(--muted) }
.chips { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:12px } .chip { font-size:12px; background:var(--soft);
  border:1px solid var(--line); border-radius:999px; padding:3px 10px; cursor:pointer } .chip.on { border-color:var(--bad); color:var(--bad) }
table.kw { width:100%; border-collapse:collapse; font-size:14px } .kw th, .kw td { text-align:left; padding:8px 10px 8px 0;
  border-bottom:1px solid var(--line) } .kw th { color:var(--muted); font-weight:500; font-size:12px; text-transform:uppercase }
.empty { color:var(--muted); padding:30px; text-align:center } .more { text-align:center; margin:10px 0 }
.toast { position:fixed; bottom:18px; left:50%; transform:translateX(-50%); background:var(--ink); color:var(--bg);
  padding:8px 14px; border-radius:8px; font-size:13px; opacity:0; transition:opacity .2s; pointer-events:none } .toast.show { opacity:1 }
@media (max-width: 860px) { .layout { grid-template-columns:1fr } aside { position:static; max-height:none } }
</style></head><body>
<header><h1>Internal links to insert</h1><p class="sub" id="sub"></p></header>
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
  <div class="f"><label>Links per page <span id="v-max"></span></label><input type="range" min="1" max="15" id="f-max"></div>
  <div class="f"><label>Match type</label><select id="f-type"><option value="all">exact + partial</option>
    <option value="exact">exact only</option><option value="partial">partial only</option></select></div>
  <div class="f" id="f-lang-wrap"><label>Language</label><select id="f-lang"></select></div>
  <div class="f" id="f-kw-wrap"><label>Keyword</label><select id="f-kw"></select></div>
  <div class="f"><label>Search</label><input type="search" id="f-q" placeholder="page, target or phrase"></div>
  <div class="actions"><button class="btn" id="reset">Reset to run defaults</button></div>
</aside>
<main>
  <div class="stats" id="stats"></div>
  <div class="tabs"><button class="tab on" data-view="pages">By page</button><button class="tab" data-view="targets">By target</button>
    <button class="tab" data-view="keywords" id="tab-kw">Keywords</button><button class="tab" data-view="rejected">Rejected</button>
    <div class="right"><button class="btn primary" id="csv">Download CSV</button></div></div>
  <div id="view"></div>
</main>
</div>
<div class="toast" id="toast">Copied</div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const rows = D.rows;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const path = u => { try { return new URL(u).pathname || "/"; } catch (e) { return u; } };
const pct = x => x == null ? "–" : Math.round(x * 100) + "%";
const REASONS = { cannibalisation:"the phrase is the source page's own topic", off_topic:"the passage is about another topic",
  weak_anchor:"not natural link text", low_value:"link value below the filter", low_score:"overall score below the filter",
  too_generic:"phrase too generic", wrong_intent:"reader expects another kind of page here",
  navigation_or_template:"list, navigation or repeated block", limit:"per-page limit - better links won",
  duplicate_phrase:"this phrase already carries a better link", not_judged:"not judged by Jev (API error)",
  filtered:"hidden by type, language, keyword or search filter" };
const KEY = "il-filters:" + D.domain;
const langs = [...new Set(rows.map(r => r.l).filter(Boolean))];
const keywords = [...new Set(rows.map(r => r.k).filter(Boolean))].sort();
let S = { ...D.defaults, type:"all", lang:"all", kw:"all", q:"", view:"pages", hidden:{} };
try { Object.assign(S, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch (e) {}

$("sub").textContent = D.domain + " · " + D.date + " · " + rows.length + " suggestions judged" +
  (D.stats["Jev cost"] ? " · Jev cost " + D.stats["Jev cost"] : "");
$("f-lang").innerHTML = '<option value="all">all</option>' + langs.map(l => `<option>${esc(l)}</option>`).join("");
if (langs.length < 2) $("f-lang-wrap").hidden = true;
$("f-kw").innerHTML = '<option value="all">all keywords</option>' + keywords.map(k => `<option>${esc(k)}</option>`).join("");
if (!keywords.length) { $("f-kw-wrap").hidden = true; $("tab-kw").hidden = true; if (S.view === "keywords") S.view = "pages"; }

function evaluate() {
  const q = S.q.trim().toLowerCase();
  for (const r of rows) {
    let why = "";
    if (r.sc == null) why = "not_judged";
    else if (r.ca * 100 >= S.can) why = "cannibalisation";
    else if (r.cx * 100 < S.ctx) why = "off_topic";
    else if (r.an * 100 < S.anc) why = "weak_anchor";
    else if (r.va * 100 < S.val) why = "low_value";
    else if (r.sc * 100 < S.score) why = r.v && r.v !== "ok" ? r.v : "low_score";
    else if ((S.type !== "all" && r.ty !== S.type) || (S.lang !== "all" && r.l !== S.lang) ||
             (S.kw !== "all" && r.k !== S.kw) ||
             (q && ![r.s, r.st, r.t, r.tt, r.a, r.c].some(x => String(x).toLowerCase().includes(q)))) why = "filtered";
    r._why = why;
  }
  const passing = rows.filter(r => !r._why).sort((a, b) => b.sc - a.sc);
  const used = new Set(), perPage = {};
  for (const r of passing) {
    const key = r.s + "\u0000" + r.a.toLowerCase();
    if (used.has(key)) { r._why = "duplicate_phrase"; continue; }
    if ((perPage[r.s] || 0) >= S.max) { r._why = "limit"; continue; }
    used.add(key); perPage[r.s] = (perPage[r.s] || 0) + 1;
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
  const where = mode === "targets"
    ? `<div class="k">On page</div><div><a href="${esc(r.s)}" target="_blank">${esc(r.st || path(r.s))}</a> <span class="url">${esc(path(r.s))}</span></div>`
    : `<div class="k">Link to</div><div><a href="${esc(r.t)}" target="_blank">${esc(r.tt || path(r.t))}</a> <span class="url">${esc(path(r.t))}</span>${r.m ? '<span class="pill">already in menu</span>' : ""}</div>`;
  return `<div class="item"><div class="steps">
    <div class="k">In the ${esc(r.pl || "text")}</div><div class="ctx">${highlight(r.c || "", r.a)}</div>
    <div class="k">Link text</div><div><span class="anchor">${esc(r.a)}</span><span class="pill">${esc(r.ty)}</span>${r.k ? `<span class="pill">keyword: ${esc(r.k)}</span>` : ""}</div>
    ${where}</div>
    <div class="metrics">${bar("score", r.sc)}${bar("context", r.cx)}${bar("anchor", r.an)}${bar("value", r.va)}${bar("cannibal.", r.ca, true)}</div>
    <div class="actions"><button class="btn" data-copy="${esc(r.a)}">Copy link text</button>
      <button class="btn" data-copy="${esc(r.t)}">Copy URL</button><button class="btn" data-copy="${esc(html(r))}">Copy HTML</button></div></div>`;
}
function groups(list, key) {
  const m = new Map();
  for (const r of list) { if (!m.has(r[key])) m.set(r[key], []); m.get(r[key]).push(r); }
  return [...m.entries()].sort((a, b) => b[1].length - a[1].length);
}
const LIMIT = 80;
let shown = LIMIT;
function render() {
  const acc = evaluate();
  const pages = new Set(acc.map(r => r.s)), targets = new Set(acc.map(r => r.t));
  const judged = rows.filter(r => r.sc != null).length;
  $("stats").innerHTML = [["Links to insert", acc.length], ["Pages getting links", pages.size],
    ["Pages linked to", targets.size], ["Avg links / page", pages.size ? (acc.length / pages.size).toFixed(1) : "0"],
    ["Share accepted", judged ? Math.round(100 * acc.length / judged) + "%" : "–"]]
    .map(([l, v]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("");
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("on", t.dataset.view === S.view));
  const v = $("view");
  if (S.view === "pages" || S.view === "targets") {
    const key = S.view === "pages" ? "s" : "t";
    const g = groups(acc, key);
    if (!g.length) { v.innerHTML = '<div class="empty">No links pass these filters - lower a threshold on the left.</div>'; return; }
    v.innerHTML = g.slice(0, shown).map(([url, list]) => {
      const title = key === "s" ? (list[0].st || path(url)) : (list[0].tt || path(url));
      const label = key === "s" ? `${list.length} link${list.length > 1 ? "s" : ""} to add here` : `linked from ${list.length} page${list.length > 1 ? "s" : ""}`;
      return `<section class="group"><div class="group-h"><div><h3>${esc(title)}</h3><a class="url" href="${esc(url)}" target="_blank">${esc(url)}</a></div>
        <span class="count">${label}</span></div>${list.map(r => item(r, S.view)).join("")}</section>`;
    }).join("") + (g.length > shown ? `<div class="more"><button class="btn" id="more">Show more (${g.length - shown} left)</button></div>` : "");
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
  for (const k of ["score", "ctx", "anc", "val", "can", "max"]) {
    $("f-" + k).value = S[k]; $("v-" + k).textContent = k === "max" ? S[k] : (k === "can" ? "< " : "≥ ") + S[k] + "%";
  }
  $("f-type").value = S.type; $("f-lang").value = S.lang; $("f-kw").value = S.kw; $("f-q").value = S.q;
  try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {}
}
function update() { shown = LIMIT; sync(); render(); }
for (const k of ["score", "ctx", "anc", "val", "can", "max"]) $("f-" + k).addEventListener("input", e => { S[k] = +e.target.value; update(); });
for (const [id, k] of [["f-type", "type"], ["f-lang", "lang"], ["f-kw", "kw"]]) $(id).addEventListener("change", e => { S[k] = e.target.value; update(); });
$("f-q").addEventListener("input", e => { S.q = e.target.value; update(); });
$("reset").addEventListener("click", () => { S = { ...S, ...D.defaults, type:"all", lang:"all", kw:"all", q:"", hidden:{} }; update(); });
document.querySelector(".tabs").addEventListener("click", e => { const t = e.target.closest(".tab"); if (t) { S.view = t.dataset.view; update(); } });
$("view").addEventListener("click", e => {
  const c = e.target.closest("[data-copy]");
  if (c) { navigator.clipboard?.writeText(c.dataset.copy).then(() => { $("toast").classList.add("show"); setTimeout(() => $("toast").classList.remove("show"), 1200); }); return; }
  const chip = e.target.closest(".chip");
  if (chip) { S.hidden[chip.dataset.reason] = !S.hidden[chip.dataset.reason]; sync(); render(); return; }
  if (e.target.id === "more") { shown += LIMIT; render(); }
});
$("csv").addEventListener("click", () => {
  const acc = evaluate(), cols = ["source_url", "source_title", "placement", "anchor", "anchor_type", "target_url", "target_title", "score", "context"];
  const q = x => '"' + String(x ?? "").replace(/"/g, '""') + '"';
  const lines = [cols.join(",")].concat(acc.map(r => [r.s, r.st, r.pl, r.a, r.ty, r.t, r.tt, r.sc, r.c].map(q).join(",")));
  const blob = new Blob(["﻿" + lines.join("\n")], { type:"text/csv" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `links-${D.domain}.csv`; a.click();
});
update();
</script></body></html>"""
