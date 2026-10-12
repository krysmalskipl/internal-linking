// internal-linking ui - the tool in the browser. server.py serves this page, its API and the files in
// data/<domain>/; every action runs the CLI as a job whose output is shown live.
"use strict";
const TOKEN = document.body.dataset.token;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const when = s => String(s || "").slice(0, 16).replace("T", " ");
const bytes = n => n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB";
const pathOf = u => { try { return new URL(u).pathname; } catch (e) { return u; } };
const fileUrl = (d, name, download) =>
  `/files/${encodeURIComponent(d)}/${name.split("/").map(encodeURIComponent).join("/")}${download ? "?download" : ""}`;

const TABS = [["overview", "Overview"], ["run", "Run"], ["verify", "Verify"], ["fanout", "Fan-out"], ["quality", "Quality"], ["files", "Files"], ["history", "History"]];
// verify.py statuses: badge class and label (an icon and words, never colour alone)
const VSTATE = { ok: ["ok", "✓ inserted"], other_text: ["warn", "! other link text"], other_place: ["warn", "! other passage"],
  outside_content: ["warn", "! menu or footer only"], missing: ["bad", "✗ not linked yet"],
  target_error: ["bad", "✗ target does not load"], page_error: ["bad", "✗ page did not load"] };

let projects = [], settings = null, current = null, view = "project", tab = "overview";
const added = [];             // domains added on this page, not on disk until their first job
const jobs = {};              // domain → the job shown for it
const forms = {};             // domain → command → option → value (kept while switching tabs)
const hidden = {};            // verification statuses filtered out
let timer = null;

async function api(method, path, body, raw) {
  const opts = { method, headers: { "X-Token": TOKEN } };
  if (body !== undefined) {
    opts.body = raw ? body : JSON.stringify(body);
    if (!raw) opts.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
function toast(msg, err) {
  const t = $("toast");
  t.textContent = msg; t.classList.toggle("err", !!err); t.classList.add("show");
  clearTimeout(t.hideTimer); t.hideTimer = setTimeout(() => t.classList.remove("show"), err ? 7000 : 2500);
}
const proj = d => projects.find(p => p.domain === d);
const blank = d => ({ domain: d, run_at: "", links: null, pages: null, stats: {}, files: [], inputs: [], logs: [], job: "",
  verified: { ok: 0, total: 0, checked: "", where: "", rows: [] } });

// ---- navigation ------------------------------------------------------------------------------
function go(hash) { if (location.hash !== hash) location.hash = hash; else route(); }
function route() {
  const [, kind, d, t] = location.hash.split("/");
  if (kind === "settings") { view = "settings"; current = null; }
  else if (kind === "d" && d) { view = "project"; current = decodeURIComponent(d); tab = TABS.some(x => x[0] === t) ? t : "overview"; }
  else { view = "project"; current = current || (projects[0] || {}).domain || null; }
  render();
}
async function refresh() {
  try { projects = await api("GET", "/api/projects"); }
  catch (e) { toast(e.message, true); return; }
  for (const p of projects) if (p.job && !(jobs[p.domain] && jobs[p.domain].id === p.job)) attach(p.domain, { id: p.job, status: "running" });
  render();
}

// ---- sidebar ----------------------------------------------------------------------------------
function renderSide() {
  const names = [...projects.map(p => p.domain), ...added.filter(d => !proj(d))];
  $("projects").innerHTML = names.length ? names.map(d => {
    const p = proj(d) || blank(d), running = jobs[d] && jobs[d].status === "running";
    return `<button class="proj ${d === current && view === "project" ? "on" : ""}" data-domain="${esc(d)}">
      <b>${esc(d)}${running ? '<i class="live" title="a job is running"></i>' : ""}</b>
      <span>${p.run_at ? "run " + esc(when(p.run_at)) + " · " + (p.links ?? "–") + " links" : "no run yet"}</span>
      ${p.verified.total ? `<span>inserted ${p.verified.ok} / ${p.verified.total}</span>` : ""}</button>`;
  }).join("") : '<p class="muted small">No domains yet - add one above and run it.</p>';
}

// ---- views ------------------------------------------------------------------------------------
function render() {
  renderSide();
  const main = $("main");
  if (view === "settings") return renderSettings(main);
  if (!current) {
    main.innerHTML = `<div class="card"><h2>Start</h2><ol class="steps">
      <li>Add your API keys in <a href="#/settings">Settings</a> (OpenRouter; TypeSafe optional).</li>
      <li>Add a domain on the left and run it - the crawl, the phrases, Jev and the report.</li>
      <li>Insert the links (by hand, or give <code>links-for-agent.md</code> to a coding agent), then verify them.</li></ol></div>`;
    return;
  }
  const p = proj(current) || blank(current), has = n => p.files.some(f => f.name === n);
  main.innerHTML = `<div class="head"><div><h1>${esc(current)}</h1>
      <p class="muted small">${p.run_at ? `run ${esc(when(p.run_at))} · ${p.links ?? "–"} links to insert · ${p.pages ?? "–"} pages` : "not run yet"}</p></div>
    <div class="actions">${has("report.html") ? `<a class="btn primary" href="${fileUrl(current, "report.html")}" target="_blank" rel="noopener">Open report</a>` : ""}
      ${has("links-for-agent.md") ? `<a class="btn" href="${fileUrl(current, "links-for-agent.md", true)}">links-for-agent.md</a>` : ""}
      ${has("links.csv") ? `<a class="btn" href="${fileUrl(current, "links.csv", true)}">links.csv</a>` : ""}</div></div>
    <div class="tabs" role="tablist">${TABS.map(([k, label]) =>
      `<button class="tab ${k === tab ? "on" : ""}" data-tab="${k}" role="tab" aria-selected="${k === tab}">${label}</button>`).join("")}</div>
    <div id="tab-body"></div>`;
  ({ overview, run: runTab, verify: verifyTab, fanout: fanoutTab, quality, files, history })[tab]($("tab-body"), p);
}

function overview(el, p) {
  const s = p.stats || {}, v = p.verified;
  el.innerHTML = `<div class="stats">
      <div class="stat"><b>${p.links ?? "–"}</b><span>links to insert</span></div>
      <div class="stat"><b>${p.pages ?? "–"}</b><span>pages crawled</span></div>
      <div class="stat"><b>${esc(s["Jev cost"] || "–")}</b><span>Jev cost</span></div>
      <div class="stat"><b>${esc(s["Embeddings cost"] || "–")}</b><span>embeddings cost</span></div>
      ${p.broken ? `<div class="stat"><b>${p.broken}</b><span>broken links to repair (report, links-for-agent.md)</span></div>` : ""}
      ${s["Fan-out cost"] ? `<div class="stat"><b>${esc(s["Fan-out cost"])}</b><span>fan-out cost</span></div>` : ""}
      <div class="stat"><b>${v.total ? `${v.ok} / ${v.total}` : "–"}</b><span>inserted (verified)</span></div></div>
    <div class="card"><h2>Next steps</h2><ol class="steps">
      <li>${p.run_at ? `Run done ${esc(when(p.run_at))}.` : "Not run yet."} <a href="#/d/${encodeURIComponent(p.domain)}/run">Run ${p.run_at ? "again" : ""} ›</a></li>
      <li>Insert the links: from the report, or give <code>links-for-agent.md</code> to a coding agent (Claude Code, Antigravity…)
        in the site's repository - <i>"Insert the links from links-for-agent.md"</i>.</li>
      <li>${v.total ? `Checked ${esc(when(v.checked))} on ${esc(v.where === "live" ? "the live site" : v.where === "crawl" ? "the last crawl" : v.where)}: ${v.ok} of ${v.total} inserted.` : "Not verified yet."}
        <a href="#/d/${encodeURIComponent(p.domain)}/verify">Verify ›</a></li></ol></div>
    <div id="job"></div>`;
  renderJob();
}

// a form field bound to forms[domain][command][name]
function value(cmd, name, dflt) { const f = (forms[current] || {})[cmd] || {}; return name in f ? f[name] : dflt; }
const field = (cmd, name, label, help, attrs = "") =>
  `<div class="field"><label for="${cmd}-${name}">${label}</label><input id="${cmd}-${name}" data-cmd="${cmd}" data-opt="${name}" ${attrs}
    value="${esc(value(cmd, name, ""))}">${help ? `<small>${help}</small>` : ""}</div>`;
const check = (cmd, name, label, help, disabled) =>
  `<label class="check"><input type="checkbox" data-cmd="${cmd}" data-opt="${name}" ${value(cmd, name, false) ? "checked" : ""} ${disabled ? "disabled" : ""}>
    <span>${label}${help ? `<small>${help}</small>` : ""}</span></label>`;
function pick(cmd, name, label, help, items, first) {
  const v = value(cmd, name, "");
  return `<div class="field"><label for="${cmd}-${name}">${label}</label><select id="${cmd}-${name}" data-cmd="${cmd}" data-opt="${name}">
    ${[[ "", first ], ...items].map(([k, t]) => `<option value="${esc(k)}" ${k === v ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>
    ${help ? `<small>${help}</small>` : ""}</div>`;
}
const inputs = (p, exts) => p.inputs.filter(f => exts.some(x => f.name.endsWith(x))).map(f => [f.name.replace(/^inputs\//, ""), f.name.replace(/^inputs\//, "")]);
const upload = `<div class="row"><label class="btn" for="upload">Upload a file…</label>
  <input id="upload" type="file" accept=".csv,.txt,.md" hidden><span class="muted small">keyword lists, URL lists, link lists, Search Console exports (.csv, .txt, .md)</span></div>`;

// options most runs never change sit under Advanced; its summary counts the changed ones, so none applies unseen
const ADVANCED = { run: ["sources", "targets", "sources_file", "targets_file", "keywords", "no_crawl", "no_embeddings", "dry_run",
  "no_link_check", "fanout", "semantic", "gsc", "fanout_pages", "fanout_pages_file", "fanout_max", "max_links", "delay", "workers"],
  verify: ["delay"], label: ["port"] };
const opened = {};            // command → Advanced opened or closed by hand
const changed = cmd => ADVANCED[cmd].filter(n => ![undefined, "", false].includes(((forms[current] || {})[cmd] || {})[n])).length;
const changedText = n => n ? ` · ${n} changed` : "";
function advanced(cmd, body) {
  const n = changed(cmd);
  return `<details class="advanced" data-adv="${cmd}" ${(opened[cmd] ?? n > 0) ? "open" : ""}>
    <summary>Advanced<span class="changed" id="${cmd}-changed">${changedText(n)}</span></summary>${body}
    <div class="row"><button class="btn" data-defaults="${cmd}">Back to defaults</button></div></details>`;
}

function runTab(el, p) {
  const crawled = p.files.some(f => f.name === "pages.jsonl"), lists = inputs(p, [".txt", ".csv"]);
  el.innerHTML = `<div class="card"><h2>Run</h2>
    <p class="muted small">Crawl the sitemap, find phrases, judge every pair with Jev and build the report. Settings and keys: <a href="#/settings">Settings</a>.</p>
    <div class="grid">
      ${field("run", "sitemap", "Sitemap URL", "empty: found automatically (robots.txt, /sitemap.xml…)", 'type="url" placeholder="https://…/sitemap.xml"')}
    </div>
    ${advanced("run", `<h3>Which pages</h3><div class="grid">
      ${field("run", "sources", "Pages that get links (regex on the path)", "e.g. ^/blog/ - empty: all", 'type="text" placeholder="^/blog/"')}
      ${field("run", "targets", "Pages links may point to (regex)", "e.g. /category/|/uslugi/", 'type="text"')}
      ${pick("run", "sources_file", "…or a list of pages that get links", "uploaded .txt/.csv, one URL or path per line", lists, "—")}
      ${pick("run", "targets_file", "…or a list of target pages", "", lists, "—")}
      ${pick("run", "keywords", "Keyword mode", "only the phrases in this file (keyword,target_url,match)", inputs(p, [".csv", ".txt"]), "off - phrases from titles")}
    </div>${upload}
    <h3>Options</h3><div class="grid">
      ${check("run", "no_crawl", "Reuse the downloaded pages", crawled ? "skip the crawl (faster; no check of the previous links)" : "no pages downloaded yet", !crawled)}
      ${check("run", "no_embeddings", "Skip embeddings", "no similarity, no topic map")}
      ${check("run", "dry_run", "Dry run", "count pairs and estimate the cost, no Jev calls")}
      ${check("run", "no_link_check", "Skip the broken-link check", "links in the content to pages outside the crawl are not requested")}
    </div>
    <h3>Fan-out and semantic phrases</h3>
    <p class="muted small">Links where the text answers a question instead of naming the page. Paid: one model call per hub
      (about $0.001) and two Jev calls per link found. Try a dry run first.</p><div class="grid">
      ${check("run", "fanout", "Query fan-out", "the sub-queries of hub pages, which page answers each, links to the ones without a link")}
      ${check("run", "semantic", "Semantic phrases", "link pages from paragraphs close in meaning, without their name in the text")}
      ${pick("run", "gsc", "Search Console queries (optional)", "uploaded .csv with page + query (+ clicks, impressions) - see README", inputs(p, [".csv"]), "none - sub-queries from the model only")}
      ${field("run", "fanout_pages", "Hub pages (regex on the path)", "empty: the best linked content pages", 'type="text" placeholder="^/blog/"')}
      ${pick("run", "fanout_pages_file", "…or a list of hub pages", "uploaded .txt/.csv, one URL or path per line", lists, "—")}
      ${field("run", "fanout_max", "Most hubs", "default 100", 'type="number" min="1" max="5000" placeholder="100"')}
    </div>
    <h3>Limits and speed</h3><div class="grid">
      ${field("run", "max_links", "New links per page", "default from the thresholds (3)", 'type="number" min="1" max="50" placeholder="3"')}
      ${field("run", "delay", "Seconds between downloads", "default 0.5", 'type="number" step="0.1" min="0" placeholder="0.5"')}
      ${field("run", "workers", "Parallel Jev calls", "lower it on 429 errors (default 12)", 'type="number" min="1" max="64" placeholder="12"')}
    </div>`)}
    <div class="row"><button class="btn primary" data-start="run">Start run</button>
      <button class="btn" data-start="report" title="rebuild report.html from saved results - no crawl, no API calls">Rebuild report</button>
      <button class="btn" data-start="embed" title="add page embeddings to saved results and rebuild the report">Add embeddings</button></div></div>
    <div id="job"></div>`;
  renderJob();
}

function verifyTab(el, p) {
  const lists = inputs(p, [".md", ".csv"]);
  el.innerHTML = `<div class="card"><h2>Verify the inserted links</h2>
    <p class="muted small">Fetches only the pages that should have links and checks each one: in the content, with the expected text,
      in the proposed passage, target loads.</p>
    <div class="grid">
      ${field("verify", "base_url", "Local preview", "empty: the live site · e.g. <code>astro dev</code> on http://localhost:4321", 'type="url" placeholder="http://localhost:4321"')}
      ${pick("verify", "links", "Which links", "", [["latest", "the newest list downloaded from the report (~/Downloads)"], ...lists], "the last run's links-for-agent.md")}
    </div>${upload}
    ${advanced("verify", `<div class="grid">
      ${field("verify", "delay", "Seconds between requests", "default 0.5", 'type="number" step="0.1" min="0" placeholder="0.5"')}</div>`)}
    <div class="row"><button class="btn primary" data-start="verify">Verify</button></div></div>
    <div id="job"></div><div id="results"></div>`;
  renderJob();
  renderResults(p);
}
function renderResults(p) {
  const el = $("results"), rows = p.verified.rows;
  if (!el) return;
  if (!rows.length) { el.innerHTML = ""; return; }
  const counts = rows.reduce((o, r) => (o[r.status] = (o[r.status] || 0) + 1, o), {});
  const shown = rows.filter(r => !hidden[r.status]).sort((a, b) => (a.status === "ok") - (b.status === "ok"));
  el.innerHTML = `<div class="card"><h2>Last check · ${esc(when(p.verified.checked))}</h2>
    <div class="chips">${Object.keys(VSTATE).filter(k => counts[k]).map(k =>
      `<button class="chip ${hidden[k] ? "" : "on"}" data-hide="${k}">${esc(VSTATE[k][1])} · ${counts[k]}</button>`).join("")}</div>
    <table><tr><th>Status</th><th>Page</th><th>Link text → target</th><th>Found / note</th></tr>
    ${shown.map(r => { const s = VSTATE[r.status] || ["warn", r.status];
      return `<tr><td><span class="badge ${s[0]}">${esc(s[1])}</span></td><td class="mono">${esc(pathOf(r.source_url))}</td>
        <td><b>“${esc(r.anchor)}”</b> → <span class="mono">${esc(pathOf(r.target_url))}</span></td>
        <td class="muted small">${r.found_text && r.status !== "ok" ? `found “${esc(r.found_text)}” ` : ""}${esc(r.note || "")}</td></tr>`; }).join("")}
    </table></div>`;
}

function quality(el) {
  const j = jobs[current], labelling = j && j.command === "label" && j.status === "running";
  el.innerHTML = `<div class="card"><h2>Measure the quality on your site</h2>
    <p class="muted small">Draw a sample of suggestions, label them Yes / No (about 15 minutes for 50), then measure each signal and tune the thresholds.</p>
    <h3 class="step">1. Draw a sample</h3>
    <div class="grid">${field("sample", "n", "Sample size", "pairs to label (default 50)", 'type="number" min="10" max="500" placeholder="50"')}
      ${pick("sample", "anchor_type", "Match type", "e.g. only fan-out pairs, to measure them before they join the default thresholds",
        [["exact", "exact"], ["partial", "partial"], ["fanout", "fan-out"], ["semantic", "semantic"]], "all types")}</div>
    <div class="row"><button class="btn primary" data-start="sample">Draw a sample</button></div>
    <h3 class="step">2. Label it</h3>
    <div class="row">${labelling ? `<a class="btn primary" href="http://127.0.0.1:${esc(value("label", "port", "") || 8765)}/" target="_blank" rel="noopener">Open labelling</a>
      <button class="btn danger" data-stop>Stop labelling</button>` : '<button class="btn primary" data-start="label">Start labelling</button>'}</div>
    ${advanced("label", `<div class="grid">${field("label", "port", "Labelling page port", "default 8765", 'type="number" placeholder="8765"')}</div>`)}
    <h3 class="step">3. Evaluate</h3>
    <div class="grid">
      ${field("evaluate", "target_precision", "Target precision", "share of accepted links that must be good (default 0.9)", 'type="number" step="0.01" min="0.5" max="1" placeholder="0.9"')}
      ${check("evaluate", "write_config", "Save the new thresholds", "to config.json in the working folder")}</div>
    <div class="row"><button class="btn primary" data-start="evaluate">Evaluate</button></div></div>
    <div id="job"></div>`;
  renderJob();
}

// ---- Fan-out: the sub-queries of the hubs and who answers them (fanout.json from `run --fanout`) ----
const FSTATE = { hub: "the hub answers", linked: "a linked page answers", unlinked: "a page without a link answers", gap: "nobody answers - gap" };
const FSHORT = { hub: "hub", linked: "linked", unlinked: "no link", gap: "gap" };
const FSRC = { gsc: "GSC", "gsc+ai": "GSC + AI", ai: "AI" };
const fan = { domain: null, stamp: "", data: null, hub: "all", st: "all", src: "all" };
async function fanoutTab(el, p) {
  const file = p.files.find(f => f.name === "fanout.json");
  if (!file) {
    el.innerHTML = `<div class="card"><h2>Query fan-out</h2><p class="muted small">No fan-out results yet. Tick <i>Query fan-out</i>
      under <i>Advanced</i> in <a href="#/d/${encodeURIComponent(p.domain)}/run">Run</a> - optionally with a Search Console export.
      Each hub page gets 10-15 sub-queries; the table here shows which page answers each, which pages could get a link and
      which questions nobody on the site answers yet.</p></div>`;
    return;
  }
  if (fan.domain !== p.domain || fan.stamp !== file.modified) {
    el.innerHTML = '<p class="empty">Loading…</p>';
    try { fan.data = await (await fetch(fileUrl(p.domain, "fanout.json"))).json(); }
    catch (e) { el.innerHTML = `<p class="empty">Cannot read fanout.json: ${esc(e.message)}</p>`; return; }
    Object.assign(fan, { domain: p.domain, stamp: file.modified, hub: "all", st: "all", src: "all" });
  }
  renderFanout(el, p);
}
function renderFanout(el, p) {
  const D = fan.data, by = {};
  for (const x of D.rows) { const c = by[x.h] = by[x.h] || { hub: 0, linked: 0, unlinked: 0, gap: 0, n: 0 }; c[x.st]++; c.n++; }
  const hubs = D.hubs.filter(h => by[h.u]).sort((a, b) => by[b.u].unlinked - by[a.u].unlinked || by[b.u].gap - by[a.u].gap);
  const total = Object.keys(FSTATE).reduce((o, k) => (o[k] = D.rows.filter(x => x.st === k).length, o), {});
  const list = D.rows.filter(x => (fan.hub === "all" || x.h === fan.hub) && (fan.st === "all" || x.st === fan.st) && (fan.src === "all" || x.src === fan.src));
  const sel = (key, label, opts) => `<select data-fan="${key}" aria-label="${label}"><option value="all">${label}: all</option>` +
    opts.map(([k, l]) => `<option value="${esc(k)}" ${fan[key] === k ? "selected" : ""}>${esc(l)}</option>`).join("") + "</select>";
  const seg = c => Object.keys(FSTATE).map(k => c[k] ? `<i class="st-${k}" style="width:${100 * c[k] / c.n}%" title="${esc(FSTATE[k])}: ${c[k]}"></i>` : "").join("");
  el.innerHTML = `<div class="stats">${Object.keys(FSTATE).map(k => `<div class="stat"><b>${total[k]}</b><span>${esc(FSTATE[k])}</span></div>`).join("")}</div>
    <div class="card"><h2>Coverage per hub</h2><p class="muted small">${D.rows.length} sub-queries of ${hubs.length} hubs${D.model ? `, generated with ${esc(D.model)}` : ""}.
      Gaps are ideas for content, not pages to write for every query variant. Links found: <a href="${fileUrl(p.domain, "report.html")}" target="_blank" rel="noopener">report</a> (Fan-out tab) ·
      <a href="${fileUrl(p.domain, "fanout.csv", true)}">fanout.csv</a> · <a href="${fileUrl(p.domain, "fanout_gaps.csv", true)}">fanout_gaps.csv</a></p>
      <div class="legend">${Object.keys(FSTATE).map(k => `<span><i class="st-${k}"></i>${esc(FSTATE[k])}</span>`).join("")}</div>
      <div class="cov">${hubs.map(h => `<button data-fan-hub="${esc(h.u)}" class="${fan.hub === h.u ? "on" : ""}" title="${esc(h.u)}">${esc(h.t || pathOf(h.u))}</button>
        <div class="covbar" role="img" aria-label="${esc(Object.keys(FSTATE).map(k => `${FSTATE[k]}: ${by[h.u][k]}`).join(", "))}">${seg(by[h.u])}</div>`).join("")}</div></div>
    <div class="card"><h2>Sub-queries</h2><div class="row">${sel("hub", "Hub", hubs.map(h => [h.u, h.t || pathOf(h.u)]))}${sel("st", "State", Object.entries(FSTATE))}
      ${sel("src", "Source", Object.entries(FSRC).filter(([k]) => D.rows.some(x => x.src === k)))}<span class="muted small">${list.length} shown</span></div>
      <div class="tblwrap"><table class="fq"><tr><th>Sub-query</th><th>Source</th><th>State</th><th>Best answer elsewhere</th><th>Link</th></tr>
      ${list.slice(0, 500).map(x => `<tr><td><b>${esc(x.q)}</b><div class="muted small">${esc(x.ty)}${fan.hub === "all" ? " · " + esc(pathOf(x.h)) : ""}</div></td>
        <td class="n">${esc(FSRC[x.src] || x.src)}${x.i !== "" && x.i != null ? `<div class="muted small">${x.c} clicks · ${x.i} impr.</div>` : ""}</td>
        <td><span class="sbadge st-${x.st}">${esc(FSHORT[x.st] || x.st)}</span></td>
        <td>${x.b ? `<a href="${esc(x.b)}" target="_blank" rel="noopener">${esc(x.bt || pathOf(x.b))}</a> <span class="muted small">${Math.round((x.s || 0) * 100)}%</span>` : "–"}</td>
        <td class="muted small">${esc(x.lk || "–")}</td></tr>`).join("")}</table></div></div>`;
}
document.addEventListener("change", e => {
  const k = e.target.dataset && e.target.dataset.fan;
  if (k) { fan[k] = e.target.value; renderFanout($("tab-body"), proj(current)); }
});

function fileTable(d, list, emptyText) {
  return list.length ? `<table><tr><th>File</th><th>Size</th><th>Changed</th><th></th></tr>${list.map(f => `<tr>
    <td class="mono"><a href="${fileUrl(d, f.name)}" target="_blank" rel="noopener">${esc(f.name)}</a></td>
    <td class="n">${bytes(f.size)}</td><td class="n">${esc(when(f.modified))}</td>
    <td><a href="${fileUrl(d, f.name, true)}">download</a></td></tr>`).join("")}</table>` : `<p class="empty">${emptyText}</p>`;
}
function files(el, p) {
  el.innerHTML = `<div class="card"><h2>Results</h2>${fileTable(p.domain, p.files, "Nothing yet - run the domain first.")}</div>
    <div class="card"><h2>Uploaded inputs</h2>${fileTable(p.domain, p.inputs, "No uploads yet.")}${upload}</div>`;
}

function history(el, p) {
  el.innerHTML = `<div class="card"><h2>Job history</h2>${p.logs.length ? `<table><tr><th>When</th><th>Command</th><th></th></tr>
    ${p.logs.map(f => { const m = f.name.match(/(\d{4}-\d\d-\d\d)-(\d\d)(\d\d)(\d\d)-(\w+)\.log$/) || [];
      return `<tr><td class="n">${m[1] ? `${m[1]} ${m[2]}:${m[3]}` : esc(when(f.modified))}</td><td><b>${esc(m[5] || f.name)}</b></td>
        <td><button class="btn" data-log="${esc(f.name)}">Show</button> <a href="${fileUrl(p.domain, f.name, true)}">download</a></td></tr>`; }).join("")}</table>`
    : '<p class="empty">No jobs yet.</p>'}</div><div id="log-view"></div>`;
}

// ---- jobs: start, follow, stop -----------------------------------------------------------------
function options(cmd) { return { ...((forms[current] || {})[cmd] || {}) }; }
async function start(cmd) {
  try {
    const job = await api("POST", "/api/jobs", { command: cmd, domain: current, options: options(cmd) });
    if (!added.includes(current) && !proj(current)) added.push(current);
    attach(current, job);
    toast(`${cmd} started`);
    render();
  } catch (e) { toast(e.message, true); }
}
function attach(domain, job) {
  jobs[domain] = { lines: [], next: 0, command: "", ...job };
  poll();
}
async function poll() {
  clearTimeout(timer);
  for (const [domain, j] of Object.entries(jobs)) {
    if (j.status !== "running") continue;
    try {
      const s = await api("GET", `/api/jobs/${j.id}?from=${j.next}`);
      j.lines.push(...s.lines);
      Object.assign(j, { next: s.next, status: s.status, code: s.code, command: s.command, started: s.started, ended: s.ended, log: s.log });
      if (s.status !== "running") {
        toast(`${domain}: ${s.command} ${s.status}`, s.status === "failed");
        await refresh();
      }
    } catch (e) { j.status = "lost"; }
  }
  renderJob();
  renderSide();
  if (Object.values(jobs).some(j => j.status === "running")) timer = setTimeout(poll, 800);
}
function renderJob() {
  const el = $("job"), j = jobs[current];
  if (!el || !j) { if (el) el.innerHTML = ""; return; }
  const pre = el.querySelector("pre"), atEnd = !pre || pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  const lines = j.lines.map(l => {
    const cls = /^\s*!|warning/i.test(l) ? "warn" : /Traceback|Error\b/.test(l) ? "err" : "";
    return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l);
  }).join("\n");
  const report = j.status === "done" && ["run", "report", "embed", "verify"].includes(j.command)
    && (proj(current) || blank(current)).files.some(f => f.name === "report.html");
  el.innerHTML = `<div class="card"><div class="job-head"><div><b>${esc(j.command || "job")}</b>
      <span class="pill ${esc(j.status)}">${esc(j.status)}</span>
      <span class="muted small">${j.started ? "started " + esc(when(j.started)) : ""}${j.ended ? " · ended " + esc(when(j.ended)) : ""}${j.code != null ? " · exit code " + j.code : ""}</span></div>
    <div class="job-actions">${j.status === "running" ? '<button class="btn danger" data-stop>Stop</button>' : ""}
      ${report ? `<a class="btn primary" href="${fileUrl(current, "report.html")}" target="_blank" rel="noopener">Open report</a>` : ""}
      ${j.log ? `<a class="btn" href="${fileUrl(current, j.log, true)}">Log file</a>` : ""}</div></div>
    <pre class="log" aria-live="polite">${lines || '<span class="muted">waiting for output…</span>'}</pre></div>`;
  const out = el.querySelector("pre");
  if (atEnd) out.scrollTop = out.scrollHeight;
}

// ---- settings ---------------------------------------------------------------------------------
const CONFIG = [["score_min", "Overall score ≥", "Jev's probability that the link is good"],
  ["fanout_answer_min", "Fan-out: a passage answers ≥", "how close a paragraph must be to a sub-query to count as its answer"],
  ["fanout_merge_min", "Fan-out: merge queries ≥", "a generated sub-query this close to a Search Console query is merged into it"],
  ["semantic_min", "Semantic phrases ≥", "how close a paragraph must be to a page's name to propose a link"],
  ["meaning_score_min", "Overall score for fan-out / semantic ≥", "a stricter bar for links found by meaning, until your labels show they need none"],
  ["context_min", "Context ≥", "the passage is about the target's topic"], ["anchor_min", "Anchor quality ≥", "the phrase is natural link text"],
  ["cannibalisation_max", "Cannibalisation <", "reject when the phrase is the source page's own topic"],
  ["similarity_min", "Page similarity ≥", "0 = off; set it after `evaluate` shows it helps"],
  ["max_links_per_page", "New links per page", ""],
  ["max_links_per_target", "New links to one page", "the best ones win when many pages name the same page · 0 = no limit"],
  ["min_words", "Minimum words on a source page", ""],
  ["template_share", "Template phrase share", "a phrase on more than this share of pages is a template"]];
async function renderSettings(el) {
  if (!settings) {
    el.innerHTML = '<p class="empty">Loading…</p>';
    try { settings = await api("GET", "/api/settings"); } catch (e) { toast(e.message, true); return; }
  }
  const s = settings, keyHelp = { OPENROUTER_API_KEY: "embeddings, and Jev when there is no TypeSafe key - openrouter.ai/settings/keys",
    TYPESAFE_API_KEY: "optional: Jev on TypeSafe's own API - console.typesafe.ai; empty = Jev through OpenRouter" };
  el.innerHTML = `<div class="head"><div><h1>Settings</h1><p class="muted small">internal-linking ${esc(s.version)}</p></div></div>
    <div class="card"><h2>API keys</h2><p class="muted small">Saved to <code>${esc(s.env_path)}</code>. The page never shows a saved key - only its last characters.</p>
    ${Object.entries(s.keys).map(([k, v]) => `<div class="keyrow"><div><b class="mono">${esc(k)}</b><div class="muted small">${esc(keyHelp[k] || "")}</div></div>
      <div>${v.set ? `<span class="badge ok">✓ set ${esc(v.hint)}${v.from === "shell" ? " · from your shell" : ""}</span>` : '<span class="badge warn">not set</span>'}
        ${v.from === "shell" ? '<div class="muted small">Set in your shell - it wins over .env; change it there.</div>' : ""}
        <input type="password" data-key="${esc(k)}" placeholder="${v.set ? "paste a new key to replace it" : "paste the key"}" autocomplete="off" style="margin-top:6px"></div>
      <div>${v.set && v.from !== "shell" ? `<button class="btn danger" data-clear-key="${esc(k)}">Remove</button>` : ""}</div></div>`).join("")}
    <div class="row"><button class="btn primary" id="save-keys">Save keys</button></div></div>
    <div class="card"><h2>Thresholds</h2><p class="muted small">Saved to <code>${esc(s.config_path)}</code> (only what differs from the defaults).
      <code>evaluate --write-config</code> sets them from your labels.</p>
    <div class="grid">${CONFIG.map(([k, label, help]) => `<div class="field"><label for="cfg-${k}">${label}</label>
      <input id="cfg-${k}" data-cfg="${k}" type="number" step="${/max_links|min_words/.test(k) ? 1 : 0.01}" value="${esc(s.config[k])}">
      <small>${esc(help)}${help ? " · " : ""}default ${esc(s.defaults[k])}</small></div>`).join("")}</div>
    <div class="field" style="margin-top:12px"><label for="cfg-archive">Archives - never link targets (one regex on the URL path per line)</label>
      <textarea id="cfg-archive" data-cfg="archive_patterns">${esc((s.config.archive_patterns || []).join("\n"))}</textarea>
      <small>tags, authors, date and paged archives; categories are not archives</small></div>
    <div class="row"><button class="btn primary" id="save-config">Save thresholds</button><button class="btn" id="reset-config">Back to defaults</button></div></div>
    <div class="card"><h2>Folders</h2><p class="small">Data: <code>${esc(s.data_dir)}</code><br>Start the panel in another folder, or with
      <code>--data-dir</code>, to work on other data.</p></div>`;
}
async function saveSettings(body) {
  try { settings = await api("POST", "/api/settings", body); toast("Saved"); render(); }
  catch (e) { toast(e.message, true); }
}

// ---- events -----------------------------------------------------------------------------------
document.addEventListener("click", e => {
  const t = e.target.closest("button, a");
  if (!t) return;
  if (t.dataset.domain) { go(`#/d/${encodeURIComponent(t.dataset.domain)}/overview`); return; }
  if (t.dataset.tab) { go(`#/d/${encodeURIComponent(current)}/${t.dataset.tab}`); return; }
  if (t.dataset.start) { start(t.dataset.start); return; }
  if (t.hasAttribute("data-stop") && jobs[current]) {
    api("POST", `/api/jobs/${jobs[current].id}/stop`, {}).then(() => poll()).catch(err => toast(err.message, true));
    return;
  }
  if (t.dataset.defaults) {
    const f = (forms[current] || {})[t.dataset.defaults] || {};
    ADVANCED[t.dataset.defaults].forEach(n => delete f[n]);
    render();
    return;
  }
  if (t.dataset.fanHub) { fan.hub = fan.hub === t.dataset.fanHub ? "all" : t.dataset.fanHub; renderFanout($("tab-body"), proj(current)); return; }
  if (t.dataset.hide) { hidden[t.dataset.hide] = !hidden[t.dataset.hide]; renderResults(proj(current) || blank(current)); return; }
  if (t.dataset.log) {
    fetch(fileUrl(current, t.dataset.log)).then(r => r.text()).then(text => {
      $("log-view").innerHTML = `<div class="card"><h3 class="mono">${esc(t.dataset.log)}</h3><pre class="log">${esc(text)}</pre></div>`;
    });
    return;
  }
  if (t.id === "open-settings") { go("#/settings"); return; }
  if (t.id === "save-keys") {
    const keys = {};
    document.querySelectorAll("[data-key]").forEach(i => { if (i.value.trim()) keys[i.dataset.key] = i.value.trim(); });
    if (Object.keys(keys).length) saveSettings({ keys }); else toast("Paste a key first", true);
    return;
  }
  if (t.dataset.clearKey) { saveSettings({ keys: { [t.dataset.clearKey]: "" } }); return; }
  if (t.id === "save-config" || t.id === "reset-config") {
    const config = {};
    document.querySelectorAll("[data-cfg]").forEach(i => {
      const k = i.dataset.cfg;
      config[k] = t.id === "reset-config" ? settings.defaults[k]
        : k === "archive_patterns" ? i.value.split("\n").map(x => x.trim()).filter(Boolean) : i.value;
    });
    saveSettings({ config });
    return;
  }
  const theme = t.dataset.themeSet;
  if (theme) setTheme(theme);
});
document.addEventListener("input", e => {
  const i = e.target;
  if (!i.dataset || !i.dataset.cmd) return;
  ((forms[current] = forms[current] || {})[i.dataset.cmd] = forms[current][i.dataset.cmd] || {})[i.dataset.opt] =
    i.type === "checkbox" ? i.checked : i.value;
  const count = $(`${i.dataset.cmd}-changed`);
  if (count) count.textContent = changedText(changed(i.dataset.cmd));
});
document.addEventListener("toggle", e => { if (e.target.dataset && e.target.dataset.adv) opened[e.target.dataset.adv] = e.target.open; }, true);
document.addEventListener("change", async e => {
  if (e.target.id !== "upload" || !e.target.files.length) return;
  const f = e.target.files[0];
  try {
    await api("POST", `/api/upload/${encodeURIComponent(current)}?name=${encodeURIComponent(f.name.replace(/[^\w.-]+/g, "-"))}`, f, true);
    toast(`${f.name} uploaded`);
    if (!added.includes(current) && !proj(current)) added.push(current);
    await refresh();
  } catch (err) { toast(err.message, true); }
});
$("new-domain").addEventListener("submit", e => {
  e.preventDefault();
  const d = $("new-domain-input").value.trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0];
  if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(d)) { toast("Type a domain, e.g. example.com", true); return; }
  if (!proj(d) && !added.includes(d)) added.push(d);
  $("new-domain-input").value = "";
  go(`#/d/${encodeURIComponent(d)}/run`);
});
function setTheme(t) {
  if (t) { document.documentElement.dataset.theme = t; try { localStorage.setItem("il-theme", t); } catch (e) {} }
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.querySelectorAll("[data-theme-set]").forEach(b => b.classList.toggle("on", b.dataset.themeSet === cur));
}
window.addEventListener("hashchange", route);
setTheme();
refresh().then(route);
