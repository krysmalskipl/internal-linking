"""HTML report with links to insert - one self-contained, CMS-agnostic file."""
import html
from collections import defaultdict
from datetime import date
from urllib.parse import urlsplit

REASONS = {
    "cannibalisation": "the phrase describes the source page's own topic",
    "off_topic": "the passage is about something other than the target",
    "weak_anchor": "the phrase is not natural link text",
    "too_generic": "the phrase is too generic",
    "wrong_intent": "the reader expects a different kind of page here",
    "navigation_or_template": "list, navigation or repeated block",
    "low_score": "Jev score below the threshold",
    "limit": "per-page link limit - better suggestions won",
    "duplicate_phrase": "this phrase already carries a better link",
    "not_judged": "Jev did not judge the pair (API error)",
}

CSS = """
:root { --bg:#f7f6f3; --card:#fff; --ink:#1c1c1e; --muted:#6c6c72; --line:#e4e2dd; --accent:#2d6bd9;
        --mark:#fff0a0; --ok:#1d8448; --no:#b8412e; }
@media (prefers-color-scheme: dark) { :root { --bg:#151517; --card:#1e1e21; --ink:#ececee; --muted:#9b9ba1;
        --line:#323237; --accent:#6f9dff; --mark:#5a5020; --ok:#44b873; --no:#ec6c58; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { max-width:860px; margin:0 auto; padding:28px 16px 60px; }
h1 { font-size:24px; margin:0 0 4px } .sub { color:var(--muted); margin:0 0 20px }
.stats { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:24px }
.stat { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 14px; min-width:120px }
.stat b { display:block; font-size:20px }
.page { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; margin-bottom:14px }
.page h2 { font-size:16px; margin:0 } .url { color:var(--muted); font:12px ui-monospace,Menlo,monospace; word-break:break-all }
.link { border-top:1px solid var(--line); padding-top:12px; margin-top:12px }
.ctx { background:var(--bg); border-radius:8px; padding:10px 12px; margin:6px 0 }
mark { background:var(--mark); color:inherit; border-radius:3px; padding:0 2px }
.meta { font-size:13px; color:var(--muted) } .meta b { color:var(--ink) }
a { color:var(--accent) } details { margin-top:28px } summary { cursor:pointer; font-weight:600 }
.rej { font-size:13px; padding:6px 0; border-bottom:1px solid var(--line) } .why { color:var(--no) }
.kw { width:100%; border-collapse:collapse; font-size:14px; margin-top:10px; display:block; overflow-x:auto }
.kw th, .kw td { text-align:left; padding:6px 10px 6px 0; border-bottom:1px solid var(--line); white-space:nowrap }
.kw th { color:var(--muted); font-weight:500 }
"""


def _ctx(context: str, anchor: str) -> str:
    i = context.lower().find(anchor.lower())
    if i < 0:
        return html.escape(context)
    return (html.escape(context[:i]) + "<mark>" + html.escape(context[i:i + len(anchor)]) + "</mark>"
            + html.escape(context[i + len(anchor):]))


def write_report(path, domain: str, accepted: list[dict], rejected: list[dict], stats: dict,
                 keyword_summary: list[dict] | None = None) -> None:
    by_page = defaultdict(list)
    for r in accepted:
        by_page[(r["source_url"], r["source_title"])].append(r)
    e = html.escape
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Internal links - {e(domain)}</title><style>{CSS}</style></head><body><main>",
             f"<h1>Internal links to insert</h1>"
             f"<p class='sub'>{e(domain)} · {date.today().isoformat()}</p><div class='stats'>"]
    for label, value in stats.items():
        parts.append(f"<div class='stat'><b>{e(str(value))}</b>{e(label)}</div>")
    parts.append("</div>")
    if keyword_summary is not None:
        parts.append("<section class='page'><h2>Keywords</h2><table class='kw'><tr><th>keyword</th><th>match</th>"
                     "<th>target</th><th>found on</th><th>links</th><th>main reject reasons</th></tr>")
        for k in keyword_summary:
            auto = " <span class='meta'>(auto)</span>" if k["target_auto_picked"] else ""
            parts.append(f"<tr><td><b>{e(k['keyword'])}</b></td><td>{e(k['match'])}</td>"
                         f"<td><a href='{e(k['target_url'])}'>{e(urlsplit(k['target_url']).path or '/')}</a>{auto}</td>"
                         f"<td>{k['pages_with_phrase']}</td><td>{k['links_to_insert']}</td>"
                         f"<td class='meta'>{e(k['top_reject_reasons'])}</td></tr>")
        parts.append("</table></section>")
    if not accepted:
        parts.append("<p>No links pass the thresholds.</p>")
    for (url, title), links in sorted(by_page.items(), key=lambda kv: -len(kv[1])):
        parts.append(f"<section class='page'><h2>{e(title)}</h2>"
                     f"<div class='url'><a href='{e(url)}'>{e(urlsplit(url).path or '/')}</a></div>")
        for r in links:
            parts.append(
                f"<div class='link'><div class='meta'>{e(r['placement'])} · "
                + (f"keyword <b>{e(r['keyword'])}</b> · " if r.get("keyword") else "") + "phrase "
                f"<b>“{e(r['anchor'])}”</b> ({e(r['anchor_type'])}) → "
                f"<a href='{e(r['target_url'])}'>{e(r['target_title'])}</a></div>"
                f"<div class='ctx'>{_ctx(r['context'], r['anchor'])}</div>"
                f"<div class='meta'>score {float(r['score']):.2f} · context {float(r['jev_context']):.2f} · "
                f"anchor {float(r['jev_anchor']):.2f} · {e(urlsplit(r['target_url']).path)}</div></div>")
        parts.append("</section>")
    if rejected:
        parts.append(f"<details><summary>Rejected suggestions ({len(rejected)})</summary>")
        for r in sorted(rejected, key=lambda r: r["source_url"]):
            parts.append(f"<div class='rej'>{e(urlsplit(r['source_url']).path)} · “{e(r['anchor'])}” → "
                         f"{e(urlsplit(r['target_url']).path)} · <span class='why'>"
                         f"{e(REASONS.get(r['decision_reason'], r['decision_reason']))}</span></div>")
        parts.append("</details>")
    parts.append("</main></body></html>")
    path.write_text("".join(parts), encoding="utf-8")
