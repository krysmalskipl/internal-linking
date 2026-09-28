"""Linki wewnętrzne dla dowolnych domen - jedna komenda, bez ręcznej obsługi.

  python run.py --domain example.com
  python run.py --domain a.pl --domain b.pl --max-links 3
  python run.py --domains-file domeny.txt           # jedna domena w wierszu
  python run.py --domain example.com --no-crawl     # bez ponownego pobierania stron
  python run.py --domain example.com --dry-run      # liczba par i koszt, bez Jev

Dla każdej domeny: crawl sitemapy → frazy exact/partial w akapitach i listach (anchors.py) →
reguły deterministyczne → ocena każdej pary przez Jev (jev_pairs.py, cache) → progi z config.json
→ maks. N linków na stronę → data/<domena>/links.csv + report.html.
"""
import argparse
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import jev_pairs
from anchors import AnchorFinder
from common import ROOT, append_jsonl, data_dir, norm_url, read_jsonl, write_csv
from crawl import crawl
from jev_client import JevError
from report import write_report

COST_PER_PAIR = 0.00007    # szacunek do --dry-run (ok. 600 tokenów wejścia)


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text())


def site_suffixes(pages: list[dict]) -> set[str]:
    """Końcówki tytułów powtarzające się w wielu stronach (np. ' - Nazwa serwisu')."""
    c = Counter()
    for p in pages:
        m = re.search(r"\s[-–|]\s[^-–|]+$", p["title"])
        if m:
            c[m.group(0)] += 1
    return {s for s, n in c.items() if n >= max(3, 0.3 * len(pages))}


def clean_title(title: str, suffixes: set[str]) -> str:
    for s in suffixes:
        if title.endswith(s):
            return title[: -len(s)].strip()
    return title


def sitewide_links(pages: list[dict]) -> set[str]:
    """Linki spoza treści obecne na co najmniej połowie stron - menu i stopka."""
    c = Counter(link for p in pages for link in p.get("other_links", []))
    return {link for link, n in c.items() if n >= 0.5 * len(pages)}


def already_linked(src: dict, sitewide: set[str]) -> set[str]:
    """Cele, do których strona już linkuje: w treści, w okruszkach i w blokach tylko tej strony
    (np. powiązane wpisy). Linki z menu/stopki się nie liczą - link w treści ma inną wartość."""
    local = set(src.get("other_links", [])) - sitewide
    return set(src["content_links"]) | set(src.get("breadcrumb_links", [])) | local


def find_candidates(pages: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    """Pary (źródło, fraza, cel) z frazą w tekście, po regułach deterministycznych."""
    suffixes = site_suffixes(pages)
    sitewide = sitewide_links(pages)
    finder = AnchorFinder(pages)
    titles = [clean_title(p["title"], suffixes) for p in pages]
    sources = [i for i, p in enumerate(pages) if len(p["text"].split()) >= cfg["min_words"]]

    anchors = {}  # (źródło, cel) -> (fraza, exact/partial, indeks bloku)
    for i in sources:
        linked = already_linked(pages[i], sitewide)
        for j, t in enumerate(pages):
            # linki tylko w obrębie jednego języka (wersje /en/ itp. osobno)
            if j != i and norm_url(t["url"]) not in linked and t.get("lang", "") == pages[i].get("lang", ""):
                found = finder.find_in_blocks(pages[i].get("blocks", []), titles[i], titles[j], t["h1"], t["url"])
                if found:
                    anchors[(i, j)] = found

    # ta sama fraza do tego samego celu na wielu stronach to element szablonu (podpis autora,
    # stopka wpisu), a nie treść - nie nadaje się na link
    repeats = Counter((phrase.lower(), j) for (i, j), (phrase, _, _) in anchors.items())
    limit = max(3, cfg["template_share"] * len(sources))
    template = [k for k, (phrase, _, _) in anchors.items() if repeats[(phrase.lower(), k[1])] > limit]
    for k in template:
        del anchors[k]
    # fraza, która jest dokładną nazwą innej strony, jest zarezerwowana dla tamtej strony
    reserved = 0
    for (i, j), (phrase, kind, _) in list(anchors.items()):
        if kind == "partial" and any(
                k != j and (m := finder.find(phrase, "", titles[k], t["h1"], t["url"])) and m[1] == "exact"
                for k, t in enumerate(pages)):
            del anchors[(i, j)]
            reserved += 1

    rows = []
    for (i, j), (phrase, kind, b) in anchors.items():
        block = pages[i]["blocks"][b]
        state = jev_pairs.build_state(pages[i], titles[i], block, pages[j], titles[j], phrase)
        rows.append({
            "source_url": pages[i]["url"], "source_title": titles[i],
            "target_url": pages[j]["url"], "target_title": titles[j],
            "anchor": phrase, "anchor_type": kind, "miejsce": state["miejsce"],
            "kontekst": state["fragment_z_fraza"], "in_menu": int(norm_url(pages[j]["url"]) in sitewide),
            "_state": state,
        })
    info = {"źródeł": len(sources), "fraz z szablonu": len(template), "fraz zarezerwowanych": reserved}
    return rows, info


def judge(rows: list[dict], ddir: Path, workers: int) -> float:
    """Ocena każdej pary osobnym wywołaniem Jev, z cache w jev_pairs.jsonl."""
    cache_path = ddir / "jev_pairs.jsonl"
    cache = {r["key"]: r["response"] for r in read_jsonl(cache_path)}
    keys = [jev_pairs.cache_key(r["_state"]) for r in rows]
    todo = {k: r["_state"] for k, r in zip(keys, rows) if k not in cache}
    cost, failed = 0.0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {k: ex.submit(jev_pairs.judge, st) for k, st in todo.items()}
        for n, (k, fut) in enumerate(futures.items(), 1):
            try:
                resp = fut.result()
            except JevError as e:
                failed += 1
                print(f"  ! Jev: {str(e)[:200]}")
                continue
            append_jsonl(cache_path, {"key": k, "response": resp})
            cache[k] = resp
            cost += resp.get("usage", {}).get("cost", 0) or 0
            if n % 50 == 0:
                print(f"  ocenione pary: {n}/{len(todo)}")
    if todo and failed > 0.2 * len(todo):
        raise RuntimeError(f"Jev nie ocenił {failed} z {len(todo)} par - przerywam tę domenę.")
    for r, k in zip(rows, keys):
        if k in cache:
            r.update(jev_pairs.summarize(cache[k]))
    print(f"Jev: {len(todo) - failed} nowych par, {len(rows) - len(todo)} z cache, koszt ${cost:.4f}")
    return cost


def select(rows: list[dict], cfg: dict, max_links: int) -> None:
    """Ustawia decision (accept/reject) i decision_reason dla każdej pary."""
    for r in rows:
        r["decision_reason"] = jev_pairs.reject_reason(r, cfg) if "score" in r else "brak_oceny"
    passing = sorted((r for r in rows if not r["decision_reason"]), key=lambda r: -r["score"])
    # jedna fraza w tekście niesie jeden link; potem limit linków na stronę źródłową
    used_phrases, per_page = set(), Counter()
    for r in passing:
        key = (r["source_url"], r["anchor"].lower())
        if key in used_phrases:
            r["decision_reason"] = "duplikat_frazy"
        elif per_page[r["source_url"]] >= max_links:
            r["decision_reason"] = "limit"
        else:
            used_phrases.add(key)
            per_page[r["source_url"]] += 1
    for r in rows:
        r["decision"] = "reject" if r["decision_reason"] else "accept"


def process(domain: str, args, cfg: dict) -> dict:
    print(f"\n=== {domain}")
    ddir = data_dir(domain)
    if not args.no_crawl:
        crawl(domain, args.sitemap, args.delay)
    pages = read_jsonl(ddir / "pages.jsonl")
    if not pages or "blocks" not in pages[0]:
        raise RuntimeError("brak aktualnego pages.jsonl - uruchom bez --no-crawl")

    rows, info = find_candidates(pages, cfg)
    print(f"stron: {len(pages)}, " + ", ".join(f"{k}: {v}" for k, v in info.items())
          + f", par z frazą: {len(rows)}")
    if args.dry_run:
        cache = {r["key"] for r in read_jsonl(ddir / "jev_pairs.jsonl")}
        new = sum(jev_pairs.cache_key(r["_state"]) not in cache for r in rows)
        print(f"do oceny przez Jev: {new} (reszta w cache), szacowany koszt ${new * COST_PER_PAIR:.3f}")
        return {"domena": domain, "par": len(rows), "do oceny": new}

    cost = judge(rows, ddir, args.workers) if rows else 0.0
    select(rows, cfg, args.max_links or cfg["max_links_per_page"])
    accepted = [r for r in rows if r["decision"] == "accept"]
    rejected = [r for r in rows if r["decision"] == "reject"]

    fields = ["decision", "decision_reason", "source_url", "source_title", "miejsce", "anchor", "anchor_type",
              "target_url", "target_title", "in_menu", "score", "jev_kontekst", "jev_anchor", "jev_wartosc",
              "jev_kanibalizacja", "jev_powod", "kontekst"]
    rows.sort(key=lambda r: (r["decision"] != "accept", r["source_url"], -r.get("score", 0)))
    write_csv(ddir / "recommendations.csv", rows, fields)
    write_csv(ddir / "links.csv", accepted, fields[2:])
    pages_with = len({r["source_url"] for r in accepted})
    write_report(ddir / "report.html", domain, accepted, rejected, {
        "linków do wstawienia": len(accepted), "stron z nowymi linkami": pages_with,
        "ocenionych propozycji": len(rows), "koszt Jev": f"${cost:.3f}"})
    print(f"linków do wstawienia: {len(accepted)} na {pages_with} stronach "
          f"(odrzucone: {dict(Counter(r['decision_reason'] for r in rejected))})")
    print(f"→ {ddir / 'report.html'}\n→ {ddir / 'links.csv'}")
    return {"domena": domain, "linków": len(accepted), "stron": pages_with, "koszt": round(cost, 4)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", action="append", default=[], help="domena (można podać kilka razy)")
    ap.add_argument("--domains-file", help="plik z domenami, jedna w wierszu (# = komentarz)")
    ap.add_argument("--max-links", type=int, help="maks. nowych linków na stronę (domyślnie z config.json)")
    ap.add_argument("--no-crawl", action="store_true", help="użyj zapisanego pages.jsonl")
    ap.add_argument("--dry-run", action="store_true", help="policz pary i koszt bez wywołań Jev")
    ap.add_argument("--sitemap", help="adres sitemapy (tylko przy jednej domenie)")
    ap.add_argument("--delay", type=float, default=0.5, help="przerwa między pobraniami stron (s)")
    ap.add_argument("--workers", type=int, default=4, help="równoległe wywołania Jev")
    args = ap.parse_args()

    domains = list(args.domain)
    if args.domains_file:
        domains += [ln.strip() for ln in Path(args.domains_file).read_text().splitlines()
                    if ln.strip() and not ln.startswith("#")]
    if not domains:
        ap.error("podaj --domain albo --domains-file")
    if args.sitemap and len(domains) > 1:
        ap.error("--sitemap działa tylko z jedną domeną")

    cfg = load_config()
    summary = []
    for d in dict.fromkeys(domains):
        try:
            summary.append(process(d, args, cfg))
        except (RuntimeError, SystemExit) as e:
            print(f"! {d}: {e}")
            summary.append({"domena": d, "błąd": str(e)[:120]})
    if len(summary) > 1:
        print("\n=== podsumowanie")
        for s in summary:
            print("  " + ", ".join(f"{k}: {v}" for k, v in s.items()))


if __name__ == "__main__":
    main()
