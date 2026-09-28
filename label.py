"""Ocena próbki w przeglądarce zamiast w arkuszu.

Pokazuje po kolei propozycje z sample_to_label.csv jako pytanie "czy na stronie X dodać link
do strony Y?" z przyciskami Tak / Nie / Pomiń. Każda odpowiedź od razu zapisuje się w CSV.

  python label.py --domain example.com     # potem http://localhost:8765
"""
import argparse
import html
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import data_dir, read_csv, read_jsonl, write_csv

FIELDS = ["ok", "probability", "source_title", "target_title", "anchor", "anchor_type", "in_menu",
          "source_url", "target_url", "rank"]

PAGE = """<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ocena linków</title>
<style>
:root { --bg:#f6f5f2; --card:#fff; --ink:#1d1d1f; --muted:#6b6b70; --line:#e3e1dc; --accent:#2f6fde;
        --yes:#1f8a4c; --no:#c2412d; --mark:#fff2a8; }
@media (prefers-color-scheme: dark) { :root { --bg:#161618; --card:#1f1f22; --ink:#ececee; --muted:#9a9aa0;
        --line:#333338; --accent:#6d9cff; --yes:#3fb970; --no:#ef6a55; --mark:#5c5220; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--ink);
  font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { max-width:720px; margin:0 auto; padding:24px 16px 48px; }
.top { display:flex; justify-content:space-between; align-items:center; color:var(--muted); font-size:14px; }
.bar { height:6px; background:var(--line); border-radius:3px; margin:8px 0 24px; overflow:hidden }
.bar i { display:block; height:100%; background:var(--accent); transition:width .2s }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:22px; }
.q { font-size:20px; font-weight:600; margin:0 0 18px; line-height:1.35 }
.lbl { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin-top:14px }
a { color:var(--accent) } .snip { background:var(--bg); border-radius:8px; padding:12px 14px; margin-top:6px; font-size:15px }
mark { background:var(--mark); color:inherit; padding:0 2px; border-radius:3px }
.note { font-size:14px; color:var(--muted); margin-top:14px }
.btns { display:flex; gap:10px; margin-top:22px; flex-wrap:wrap }
button { font:inherit; font-weight:600; border:0; border-radius:10px; padding:12px 20px; cursor:pointer; flex:1; min-width:120px }
.yes { background:var(--yes); color:#fff } .no { background:var(--no); color:#fff }
.skip { background:transparent; color:var(--muted); border:1px solid var(--line) }
.nav { display:flex; justify-content:space-between; margin-top:14px; font-size:14px }
.nav button { flex:0; white-space:nowrap; padding:6px 10px; background:none; color:var(--muted); min-width:0; font-weight:400 }
.path { color:var(--muted); font-size:13px; font-family:ui-monospace,Menlo,monospace }
.help { color:var(--muted); font-size:14px; margin:0 0 18px }
.done { text-align:center; padding:40px 22px }
</style></head><body><main>
<p class="help">Oceń, czy proponowany link ma sens dla czytelnika. <b>Tak</b> = sam byś go wstawił.
<b>Nie</b> = temat niezwiązany albo link nic nie wnosi. Skróty klawiszowe: T / N / spacja (pomiń).</p>
<div class="top"><span id="count"></span><span id="stat"></span></div>
<div class="bar"><i id="prog"></i></div>
<div id="app"></div>
</main><script>
const items = __ITEMS__;
let i = items.findIndex(x => x.ok === "");
if (i < 0) i = items.length;
const path = u => esc(new URL(u).pathname);
const esc = s => s.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function render() {
  const done = items.filter(x => x.ok !== "").length;
  document.getElementById("count").textContent = `Oceniono ${done} z ${items.length}`;
  document.getElementById("stat").textContent = `Tak: ${items.filter(x=>x.ok==="1").length} · Nie: ${items.filter(x=>x.ok==="0").length}`;
  document.getElementById("prog").style.width = (100 * done / items.length) + "%";
  const app = document.getElementById("app");
  if (i >= items.length) {
    app.innerHTML = `<div class="card done"><p class="q">Gotowe - dzięki!</p>
      <p>Odpowiedzi są zapisane. Wróć do Claude i napisz, że skończyłeś.</p>
      <div class="nav" style="justify-content:center"><button onclick="i=0;render()">← wróć do początku</button></div></div>`;
    return;
  }
  const it = items[i];
  const prev = it.ok === "1" ? "Twoja odpowiedź: Tak" : it.ok === "0" ? "Twoja odpowiedź: Nie" : "";
  app.innerHTML = `<div class="card">
    <p class="q">Czy na stronie „${esc(it.source_title)}” dodać link do „${esc(it.target_title)}”?</p>
    <div class="lbl">Strona, na której byłby link</div>
    <div><a href="${it.source_url}" target="_blank">${esc(it.source_title)}</a> <span class="path">${path(it.source_url)}</span></div>
    <div class="lbl">Strona, do której prowadziłby link</div>
    <div><a href="${it.target_url}" target="_blank">${esc(it.target_title)}</a> <span class="path">${path(it.target_url)}</span></div>
    <div class="lbl">Fraza w tekście, na którą trafiłby link (${it.anchor_type === "exact" ? "exact match" : "partial match"})</div>
    <div class="snip">${it.snippet || esc(it.anchor)}</div>
    ${it.in_menu === "1" ? `<div class="note">Ta strona jest już w menu serwisu - pytanie dotyczy dodatkowego linku w treści.</div>` : ""}
    <div class="btns"><button class="yes" onclick="answer('1')">Tak (T)</button>
      <button class="no" onclick="answer('0')">Nie (N)</button>
      <button class="skip" onclick="answer('')">Nie wiem / pomiń</button></div>
    <div class="nav"><button onclick="go(-1)">← poprzednia</button><span style="color:var(--muted)">${prev}</span>
      <button onclick="go(1)">następna →</button></div></div>`;
}
function go(d) { i = Math.max(0, Math.min(items.length, i + d)); render(); }
async function answer(v) {
  items[i].ok = v;
  await fetch("/label", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({idx:i, ok:v})});
  i++; render();
}
document.addEventListener("keydown", e => {
  if (e.key === "t" || e.key === "T") answer("1");
  else if (e.key === "n" || e.key === "N") answer("0");
  else if (e.key === " ") { e.preventDefault(); answer(""); }
});
render();
</script></body></html>"""


def snippet(text: str, anchor: str, width: int = 160) -> str:
    """Fragment tekstu źródła z zaznaczoną frazą anchora (HTML)."""
    if not anchor:
        return ""
    m = re.search(re.escape(anchor), text)
    if not m:
        return ""
    start, end = max(0, m.start() - width), min(len(text), m.end() + width)
    before, after = text[start:m.start()], text[m.end():end]
    return (("…" if start else "") + html.escape(before) + f"<mark>{html.escape(anchor)}</mark>"
            + html.escape(after) + ("…" if end < len(text) else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    ddir = data_dir(args.domain)
    path = ddir / "sample_to_label.csv"
    rows = read_csv(path)
    texts = {p["url"]: p.get("text_free", p["text"]).replace(" ¶ ", " ") for p in read_jsonl(ddir / "pages.jsonl")}
    items = [{**r, "snippet": snippet(texts.get(r["source_url"], ""), r.get("anchor", ""))} for r in rows]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGE.replace("__ITEMS__", json.dumps(items, ensure_ascii=False).replace("</", "<\\/")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            rows[data["idx"]]["ok"] = items[data["idx"]]["ok"] = data["ok"]
            write_csv(path, rows, FIELDS)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    print(f"Ocena {len(rows)} par: http://localhost:{args.port}  (Ctrl+C kończy)")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
