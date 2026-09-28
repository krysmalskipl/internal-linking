"""Krok 2: pages.jsonl → Jev → recommendations.csv

Dla każdej strony źródłowej wybiera kandydatów, dla których w jej treści jest fraza nadająca się
na anchor (exact albo partial, patrz anchors.py). Pomija strony, do których źródło już linkuje
w treści, okruszkach lub blokach tylko tej strony (cele z menu/stopki zostają). Jev wybiera
z nich najlepszy cel linku wewnętrznego.
Odpowiedzi trafiają do cache jev_raw.jsonl, więc ponowne uruchomienie nie kosztuje drugi raz.

  python recommend.py --domain example.com --dry-run
  python recommend.py --domain example.com --limit 3
  python recommend.py --domain example.com
"""
import argparse
import hashlib
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from anchors import AnchorFinder
from common import append_jsonl, data_dir, norm_url, read_jsonl, url_folder, write_csv
from jev_client import JevError, decide

MAX_OPTIONS = 254          # 255 minus opcja "brak"
MAX_TEXT_CHARS = 15000
TOKEN_BUDGET = 30000       # limit Jev to 32k - zostawiamy zapas
CHARS_PER_TOKEN = 3        # ostrożny szacunek dla polskiego tekstu
NONE_KEY = "brak"
TOP_N = 3

INSTRUCTIONS = (
    "Jesteś redaktorem SEO. Na podstawie treści strony źródłowej wybierz stronę z listy, "
    "która jest najbardziej trafnym celem linku wewnętrznego z tej treści - taką, w którą "
    "czytelnik tej strony najpewniej chciałby kliknąć, bo rozwija lub uzupełnia temat. "
    "Przy każdej stronie podana jest fraza z treści, na którą trafiłby link - oceń, czy link "
    "na tej frazie w tym miejscu jest naturalny. Wybierz 'brak', jeśli żaden nie pasuje."
)

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


def describe(p: dict, suffixes: set[str], anchor: str) -> str:
    parts = [clean_title(p["title"], suffixes)]
    if p["h1"] and p["h1"].lower() not in parts[0].lower():
        parts.append(p["h1"])
    if p["meta"]:
        parts.append(p["meta"][:150])
    parts.append(urlsplit(p["url"]).path)
    parts.append(f"fraza w tekście: „{anchor}”")
    return " | ".join(parts)


def sitewide_links(pages: list[dict]) -> set[str]:
    """Linki spoza treści obecne na co najmniej połowie stron - menu i stopka."""
    c = Counter(link for p in pages for link in p.get("other_links", []))
    return {link for link, n in c.items() if n >= 0.5 * len(pages)}


def already_linked(src: dict, sitewide: set[str]) -> set[str]:
    """Cele, do których strona już linkuje: w treści, w okruszkach i w blokach tylko tej strony
    (np. powiązane wpisy). Linki z menu/stopki się nie liczą - link w treści ma inną wartość."""
    local = set(src.get("other_links", [])) - sitewide
    return set(src["content_links"]) | set(src.get("breadcrumb_links", [])) | local


def preselect(src_idx: int, pages: list[dict], sim, sitewide: set[str], anchors: dict) -> list[int]:
    """Kandydaci z frazą w treści źródła, do których źródło jeszcze nie linkuje."""
    src = pages[src_idx]
    linked = already_linked(src, sitewide)
    cands = [i for i, p in enumerate(pages)
             if i != src_idx and norm_url(p["url"]) not in linked and (src_idx, i) in anchors]
    if len(cands) <= MAX_OPTIONS:
        return cands
    folder = url_folder(src["url"])

    def score(i: int) -> float:
        bonus = 0.05 if folder and url_folder(pages[i]["url"]) == folder else 0.0
        return sim[src_idx, i] + bonus

    return sorted(cands, key=score, reverse=True)[:MAX_OPTIONS]


def build_request(src: dict, cand_pages: list[dict], suffixes: set[str],
                  phrases: list[str]) -> tuple[dict, dict, dict]:
    options = {f"t{i:03d}": p for i, p in enumerate(cand_pages, 1)}
    criteria = {k: describe(p, suffixes, a) for (k, p), a in zip(options.items(), phrases)}
    criteria[NONE_KEY] = "Żadna z tych stron nie jest naturalnym, wartościowym dla czytelnika rozwinięciem tej treści."
    questions = {"cel": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": criteria}}
    state = {"url": src["url"], "tytul": clean_title(src["title"], suffixes), "h1": src["h1"], "tresc": ""}

    overhead = len(json.dumps({"state": state, "questions": questions}, ensure_ascii=False))
    room = TOKEN_BUDGET * CHARS_PER_TOKEN - overhead
    if room < 500:
        raise ValueError(f"Za dużo kandydatów dla {src['url']} - opisy opcji zajmują cały limit tokenów")
    state["tresc"] = src["text"][: min(MAX_TEXT_CHARS, room)]
    return state, questions, {k: p["url"] for k, p in options.items()}


def cache_key(src: dict, targets: dict) -> str:
    h = hashlib.sha1()
    h.update(src["text"].encode())
    h.update(json.dumps(sorted(targets.values())).encode())
    return f"{norm_url(src['url'])}#{h.hexdigest()[:12]}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--limit", type=int, help="przetwórz tylko N pierwszych stron źródłowych")
    ap.add_argument("--min-words", type=int, default=150,
                    help="strony z mniejszą liczbą słów nie są źródłami (listy, kategorie)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="bez wywołań API, tylko szacunek tokenów")
    args = ap.parse_args()

    ddir = data_dir(args.domain)
    pages = read_jsonl(ddir / "pages.jsonl")
    if not pages:
        raise SystemExit(f"Brak {ddir / 'pages.jsonl'} - uruchom najpierw crawl.py")
    by_url = {p["url"]: p for p in pages}
    suffixes = site_suffixes(pages)
    sitewide = sitewide_links(pages)
    finder = AnchorFinder(pages)

    tfidf = TfidfVectorizer(min_df=1, max_df=0.8, sublinear_tf=True)
    sim = cosine_similarity(tfidf.fit_transform([f"{p['title']} {p['h1']} {p['text']}" for p in pages]))

    sources = [i for i, p in enumerate(pages) if len(p["text"].split()) >= args.min_words]
    if args.limit:
        sources = sources[: args.limit]

    anchors = {}  # (źródło, cel) -> (fraza, exact/partial)
    for i in sources:
        src_title = clean_title(pages[i]["title"], suffixes)
        for j, t in enumerate(pages):
            if j != i:
                found = finder.find(pages[i]["text"], src_title, clean_title(t["title"], suffixes), t["h1"], t["url"])
                if found:
                    anchors[(i, j)] = found
    print(f"stron: {len(pages)}, źródeł (≥ {args.min_words} słów): {len(sources)}")

    cache_path = ddir / "jev_raw.jsonl"
    cache = {r["key"]: r for r in read_jsonl(cache_path)}
    jobs, skipped = [], 0
    for i in sources:
        cand = preselect(i, pages, sim, sitewide, anchors)
        if not cand:
            skipped += 1
            continue
        state, questions, targets = build_request(pages[i], [pages[j] for j in cand], suffixes,
                                                  [anchors[(i, j)][0] for j in cand])
        tokens = len(json.dumps({"state": state, "questions": questions}, ensure_ascii=False)) // CHARS_PER_TOKEN
        jobs.append((pages[i], state, questions, targets, cache_key(pages[i], targets), tokens))
    print(f"stron z co najmniej jednym celem z frazą w tekście: {len(jobs)} (bez żadnego: {skipped}), "
          f"par do oceny: {sum(len(j[3]) for j in jobs)}")
    if not jobs:
        raise SystemExit("Brak par z frazą w tekście - nie ma czego wysłać do Jev.")

    if args.dry_run:
        for src, _, _, targets, key, tokens in jobs:
            print(f"  {src['url']}  kandydatów: {len(targets)}  ~{tokens} tokenów"
                  f"{'  (w cache)' if key in cache else ''}")
        print(f"\nmaks. ~{max(j[5] for j in jobs)} tokenów na zapytanie (limit Jev: 32 000)")
        example = ddir / "example_request.json"
        example.write_text(json.dumps({"state": jobs[0][1], "questions": jobs[0][2]}, ensure_ascii=False, indent=2))
        print(f"przykładowe zapytanie → {example}")
        return

    todo = [j for j in jobs if j[4] not in cache]
    print(f"w cache: {len(jobs) - len(todo)}, do zapytania: {len(todo)}")

    def run(job):
        src, state, questions, targets, key, _ = job
        return key, src["url"], targets, decide(state, questions)

    new_cost = 0.0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in [ex.submit(run, j) for j in todo]:
            try:
                key, src_url, targets, resp = fut.result()
            except JevError as e:
                raise SystemExit(str(e))
            rec = {"key": key, "source": src_url, "targets": targets, "response": resp}
            append_jsonl(cache_path, rec)
            cache[key] = rec
            cost = resp.get("usage", {}).get("cost", 0) or 0
            new_cost += cost
            ans = resp["answers"]["cel"]
            top = targets.get(ans["choice"], NONE_KEY)
            print(f"  {src_url}\n    → {top}  (pewność {ans.get('confidence', 0):.2f}, ${cost:.5f})")

    rows = []
    for src, _, _, targets, key, _ in jobs:
        resp = cache[key]["response"]
        ans = resp["answers"]["cel"]
        probs = ans.get("probabilities", {})
        ranked = sorted(((k, v) for k, v in probs.items() if k != NONE_KEY), key=lambda kv: -kv[1])[:TOP_N]
        for rank, (k, p) in enumerate(ranked, 1):
            target = by_url[targets[k]]
            phrase, kind = anchors[(pages.index(src), pages.index(target))]
            rows.append({
                "source_url": src["url"],
                "source_title": clean_title(src["title"], suffixes),
                "target_url": target["url"],
                "target_title": clean_title(target["title"], suffixes),
                "rank": rank,
                "probability": round(p, 4),
                "p_none": round(probs.get(NONE_KEY, 0), 4),
                "jev_choice": "brak" if ans["choice"] == NONE_KEY else "cel",
                "confidence": round(ans.get("confidence", 0), 4),
                "anchor": phrase,
                "anchor_type": kind,
                "in_menu": int(norm_url(target["url"]) in sitewide),
            })

    out = ddir / "recommendations.csv"
    write_csv(out, rows, list(rows[0].keys()) if rows else ["source_url"])
    total_cost = sum((cache[j[4]]["response"].get("usage", {}).get("cost", 0) or 0) for j in jobs)
    print(f"\n{len(rows)} rekomendacji dla {len(jobs)} stron → {out}")
    print(f"koszt tego uruchomienia: ${new_cost:.5f}   łączny koszt odpowiedzi w cache: ${total_cost:.5f}")


if __name__ == "__main__":
    main()
