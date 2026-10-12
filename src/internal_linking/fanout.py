"""Query fan-out: the searches a hub page and its neighbours should answer, and who answers them.

A page that nobody names gets no link from phrase matching. Fan-out starts from the questions
instead: for each hub (by default the best-linked content pages, up to --fanout-max) it takes the
real search queries of that page from a Google Search Console export (--gsc, optional) and asks a
model on OpenRouter for 10-15 sub-queries a searcher on the topic also makes (FANOUT_MODEL,
deepseek/deepseek-v4-pro by default; one call per hub, cached in fanout.jsonl). GSC queries come
first; a generated one close in meaning to a GSC query (fanout_merge_min) is merged into it.

Every sub-query is then matched to the site's paragraphs (passages.py): the hub answers it itself,
a page the hub already links to answers it, a page the hub does not link to answers it - a link
opportunity - or no page does - a content gap, reported as an idea, not as a page to generate for
every query variant. Opportunities become ordinary candidates: the link goes in the hub's paragraph
closest to the sub-query, Jev picks the link text among that paragraph's phrases, and the pair is
judged, thresholded and limited like any other.

--semantic does the same without sub-queries: each target's name is matched to the paragraphs of
other pages, so a page can get links from text that never names it.
"""
import csv
import hashlib
import io
import json
import re
from collections import Counter
from contextlib import closing
from pathlib import Path

import numpy as np

from . import passages as pas
from .common import Progress, append_jsonl, norm_url, parallel, read_jsonl, write_csv
from .jev import pairs
from .jev.client import FatalApiError, JevError, api_key, env, jev_endpoint, jev_version, post

MODEL_DEFAULT = "deepseek/deepseek-v4-pro"   # pinned: the model is part of the cache key
API_URL_DEFAULT = "https://openrouter.ai/api/v1/chat/completions"
PROMPT_VERSION = "fanout-1"
TYPES = ["related", "comparison", "cost", "how-to", "audience", "local", "definition", "problem"]
VALID = (5, 20)          # sub-queries accepted from the model (10-15 asked)
GSC_PER_HUB = 30         # top GSC queries per hub, by impressions
PER_HUB = 6              # link opportunities per hub sent to Jev, best first
SEMANTIC_PER_TARGET = 3  # source passages per target sent to Jev, best first
CONTENT_CHARS = 1500
COST_PER_HUB = 0.0003    # estimate for --dry-run: about 800 tokens in and 400 out (DeepSeek V4 Pro)
COST_PER_PASSAGE = 0.000002
LANG_NAMES = {"pl": "Polish", "en": "English"}
STATES = ["hub", "linked", "unlinked", "gap"]
FIELDS = ["hub_url", "hub_title", "subquery", "type", "source", "clicks", "impressions", "state", "best_url",
          "best_title", "similarity", "hub_similarity", "best_passage", "link"]
GAP_FIELDS = ["hub_url", "hub_title", "subquery", "type", "source", "clicks", "impressions", "best_url", "similarity"]

SYSTEM = """You plan the query fan-out of a web page: the searches a person interested in the page's main topic \
also makes - the sub-questions an AI search engine splits the topic into before it answers.
Return 10-15 sub-queries:
- in the page's language, written the way people type them into a search engine (2-8 words, no quotes);
- each one a different need: related subtopics, comparisons, costs, how-to steps, who it is for, local intent, \
definitions, problems - only the types that fit the topic;
- about the topic, not about this website or brand; no navigational queries.
type is one of: related, comparison, cost, how-to, audience, local, definition, problem."""
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["subqueries"], "properties": {
    "subqueries": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["query", "type"],
        "properties": {"query": {"type": "string"}, "type": {"type": "string", "enum": TYPES}}}}}}


# ---- hubs -------------------------------------------------------------------------------------

def inbound_counts(pages: list[dict]) -> Counter:
    """Other pages linking to each page from their content."""
    c = Counter()
    for p in pages:
        c.update(set(p.get("content_links", [])) - {norm_url(p["url"])})
    return c


def pick_hubs(site: dict, pattern: str | None = None, url_list: set[str] | None = None, max_n: int = 100) -> list[int]:
    """Content pages that may get links (not listings or archives, at least min_words), optionally
    only those matching --fanout-pages / --fanout-pages-file, the best linked first."""
    from .pipeline import url_matches  # pipeline runs fan-out

    pages = site["pages"]
    inbound = inbound_counts(pages)
    hubs = [i for i in site["sources"] if url_matches(pages[i]["url"], pattern, url_list)]
    hubs.sort(key=lambda i: (-inbound[norm_url(pages[i]["url"])], pages[i]["url"]))
    return hubs[:max_n]


# ---- Google Search Console export -------------------------------------------------------------

COLUMNS = {
    "page": {"page", "pages", "url", "landing page", "address", "top pages", "strona", "strony", "adres", "adres url",
             "strona docelowa", "najczęstsze strony"},
    "query": {"query", "queries", "search query", "keyword", "top queries", "zapytanie", "zapytania", "fraza",
              "najczęstsze zapytania"},
    "clicks": {"clicks", "url clicks", "kliknięcia", "klikniecia"},
    "impressions": {"impressions", "wyświetlenia", "wyswietlenia"},
}


def header(name: str) -> str:
    return " ".join(name.strip().strip('"').lower().replace("_", " ").split())


def number(value: str) -> int:
    """'1 234', '1,234', '12,5' or '' → int."""
    v = str(value or "").replace("\xa0", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(,\d{3})+", v):
        v = v.replace(",", "")
    try:
        return int(round(float(v.replace(",", "."))))
    except ValueError:
        return 0


def load_gsc(path: str, domain: str) -> dict[str, list[dict]]:
    """Normalised page URL → its queries [{query, clicks, impressions}], most impressions first.
    Reads exports with a page and a query column (Search Analytics for Sheets, the BigQuery bulk
    export, an API script): comma, semicolon or tab separated, English or Polish column names; rows
    of the same page and query (one per day in BigQuery) are summed."""
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        raise RuntimeError(f"{path}: the file is empty")
    head = [header(h) for h in rows[0]]
    col = {field: next((n for n, h in enumerate(head) if h in names), None) for field, names in COLUMNS.items()}
    for field, examples in (("page", "Page, URL, Strona"), ("query", "Query, Zapytanie")):
        if col[field] is None:
            raise RuntimeError(f"{path}: no {field} column - expected one named e.g. {examples}; the file has: "
                               f"{', '.join(rows[0])[:200]}. The plain Search Console export does not pair pages "
                               f"with queries - see README, Fan-out")
    base = f"https://{domain.removeprefix('https://').removeprefix('http://').strip('/')}"
    totals: dict[tuple, dict] = {}
    for r in rows[1:]:
        cell = lambda field: r[col[field]] if col[field] is not None and col[field] < len(r) else ""  # noqa: E731
        page, query = cell("page").strip(), " ".join(cell("query").split())
        if not page or not query:
            continue
        url = norm_url(page if "://" in page else base + "/" + page.lstrip("/"))
        t = totals.setdefault((url, query.lower()), {"query": query, "clicks": 0, "impressions": 0})
        t["clicks"] += number(cell("clicks"))
        t["impressions"] += number(cell("impressions"))
    if not totals:
        raise RuntimeError(f"{path}: no rows with both a page and a query")
    out: dict[str, list[dict]] = {}
    for (url, _), t in totals.items():
        out.setdefault(url, []).append(t)
    for queries in out.values():
        queries.sort(key=lambda t: (-t["impressions"], -t["clicks"], t["query"]))
    return out


# ---- sub-queries from the model ---------------------------------------------------------------

def model() -> str:
    return env("FANOUT_MODEL") or MODEL_DEFAULT


def model_input(page: dict, title: str, lang: str) -> str:
    return json.dumps({"language": LANG_NAMES.get(lang, lang), "title": title, "h1": page.get("h1", ""),
                       "description": page.get("meta", "")[:300], "content_start": page.get("text", "")[:CONTENT_CHARS]},
                      ensure_ascii=False, indent=1)


def cache_key(text: str, model_id: str) -> str:
    return hashlib.sha1(f"{model_id}\n{PROMPT_VERSION}\n{text}".encode()).hexdigest()


def parse_subqueries(content: str) -> list[dict]:
    """The model's JSON → [{query, type}]; anything else raises JevError, so it is never cached."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip())
    try:
        data = json.loads(text)
    except ValueError:
        raise JevError(f"the answer is not JSON: {text[:200]}") from None
    items = data.get("subqueries") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise JevError(f"no list of sub-queries: {text[:200]}")
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("query"), str):
            raise JevError(f"unexpected item: {json.dumps(it, ensure_ascii=False)[:200]}")
        q = " ".join(it["query"].split()).strip(" \"'„”")
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append({"query": q, "type": it.get("type") if it.get("type") in TYPES else "related"})
    if not VALID[0] <= len(out) <= VALID[1]:
        raise JevError(f"{len(out)} sub-queries, expected 10-15")
    return out


REASONING_REQUIRED: set[str] = set()  # models that refuse to switch reasoning off (e.g. Gemini 3.x Flash)


def ask_model(text: str) -> tuple[list[dict], float]:
    """One call: the hub's name and start → its sub-queries, and the cost in USD. Reasoning is off
    (or minimal where a model requires it) - a list of queries needs none, and it is billed; the
    cheapest provider that supports JSON output answers (`sort: price` - the default routing may
    pick one several times dearer)."""
    m = model()
    body = {"model": m, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "fanout", "strict": True, "schema": SCHEMA}},
            "provider": {"sort": "price", "require_parameters": True}, "temperature": 0.3}
    url = env("FANOUT_API_URL") or API_URL_DEFAULT
    try:
        resp = post(url, {**body, "reasoning": {"effort": "minimal"} if m in REASONING_REQUIRED else {"enabled": False}})
    except JevError as e:
        if e.status != 400 or "reasoning" not in str(e).lower() or m in REASONING_REQUIRED:
            raise
        REASONING_REQUIRED.add(m)
        resp = post(url, {**body, "reasoning": {"effort": "minimal"}})
    content = (((resp.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    return parse_subqueries(content), float((resp.get("usage") or {}).get("cost") or 0)


def call_all(fn, todo: dict, workers: int, label: str, unit: str):
    """(key, result) for every item as its paid call finishes. A key, credit or model problem stops
    everything after the first answer; other errors skip the item, and more than 20% of them stop
    the domain. Leaving early cancels the calls not sent yet (common.parallel)."""
    failed = 0
    progress = Progress(label, len(todo), unit)
    with closing(parallel(lambda k: fn(todo[k]), list(todo), workers)) as answers:
        for k, result, err in answers:
            if isinstance(err, JevError) and err.fatal:
                raise FatalApiError(f"{label}: {err}\nstopped before sending the remaining {unit}; "
                                    f"{progress.done} answers are cached and will not be paid for again")
            if err is not None:
                if not isinstance(err, JevError):
                    raise err
                failed += 1
                print(f"  ! {label}: {str(err)[:200]}")
                continue
            progress.step()
            yield k, result
    if todo and failed > 0.2 * len(todo):
        raise RuntimeError(f"{label} failed on {failed} of {len(todo)} {unit} - stopping this domain")


def subqueries(hubs: list[int], site: dict, ddir: Path, workers: int) -> tuple[dict[int, list[dict]], float]:
    """Hub → generated sub-queries, from the cache or one model call per hub."""
    pages, titles, m = site["pages"], site["titles"], model()
    path = ddir / "fanout.jsonl"
    cache = {r["key"]: r["subqueries"] for r in read_jsonl(path)}
    texts = {h: model_input(pages[h], titles[h], pages[h]["lang"]) for h in hubs}
    keys = {h: cache_key(t, m) for h, t in texts.items()}
    todo = {keys[h]: texts[h] for h in hubs if keys[h] not in cache}
    cost = 0.0
    if todo:
        api_key()  # fail fast with a clear message when the key is missing
        print(f"asking {m} for the sub-queries of {len(todo)} hubs ({len(hubs) - len(todo)} cached)...")
        for k, (items, c) in call_all(ask_model, todo, workers, "fan-out model", "hubs"):
            append_jsonl(path, {"key": k, "model": m, "subqueries": items})
            cache[k] = items
            cost += c
    return {h: cache[keys[h]] for h in hubs if keys[h] in cache}, cost


def merge(gsc: list[dict], ai: list[dict], vec: dict[str, np.ndarray], minimum: float) -> list[dict]:
    """One list of sub-queries per hub: GSC queries first (near-identical ones folded into the one
    with more impressions), then generated ones that are not close to any already on the list."""
    out: list[dict] = []

    def closest(q: str) -> tuple[dict | None, float]:
        scored = [(o, float(vec[o["query"]] @ vec[q])) for o in out]
        return max(scored, key=lambda x: x[1], default=(None, 0.0))

    for g in gsc:
        same, s = closest(g["query"])
        if same is not None and s >= minimum:
            same["clicks"] += g["clicks"]
            same["impressions"] += g["impressions"]
        else:
            out.append({**g, "type": "search", "source": "gsc"})
    for a in ai:
        same, s = closest(a["query"])
        if same is not None and s >= minimum:
            if same["source"] == "gsc":
                same.update(source="gsc+ai", type=a["type"])
        else:
            out.append({"query": a["query"], "type": a["type"], "source": "ai", "clicks": "", "impressions": ""})
    return out


# ---- matching and candidates ------------------------------------------------------------------

def target_ok(site: dict, targets_re: str | None, targets_list: set[str] | None) -> np.ndarray:
    """Pages a link may point to: not archives, within --targets / --targets-file."""
    from .pipeline import url_matches

    return np.array([j not in site["archives"] and url_matches(p["url"], targets_re, targets_list)
                     for j, p in enumerate(site["pages"])], dtype=bool)


def snippet(page: dict, block: int, n: int = 300) -> str:
    raw = page["blocks"][block].get("raw") or page["blocks"][block]["text"]
    return raw if len(raw) <= n else raw[:n].rsplit(" ", 1)[0] + "…"


def coverage(site: dict, hubs: list[int], queries: dict[int, list[dict]], qvec: dict[str, np.ndarray],
             found: list[dict], pmat: np.ndarray, ok: np.ndarray, cfg: dict) -> list[dict]:
    """Every sub-query of every hub with the best answering passage and its state."""
    from .pipeline import already_linked

    pages = site["pages"]
    page_of = np.array([x["page"] for x in found], dtype=int)
    lang_of = np.array([pages[i]["lang"] for i in range(len(pages))])
    minimum = cfg["fanout_answer_min"]
    items = []
    for h in hubs:
        linked = already_linked(pages[h], site["sitewide"])
        allowed = ok & (lang_of == pages[h]["lang"])
        allowed[h] = False
        on_hub, elsewhere = page_of == h, allowed[page_of] if len(found) else np.zeros(0, dtype=bool)
        for q in queries.get(h, []):
            scores = pmat @ qvec[q["query"]] if len(found) else np.zeros(0)
            n_hub, s_hub = pas.best(scores, on_hub)
            n_best, s_best = pas.best(scores, elsewhere)
            item = {**q, "hub": h, "hub_similarity": s_hub, "hub_block": found[n_hub]["block"] if n_hub >= 0 else -1,
                    "best": -1, "similarity": s_best if n_best >= 0 else "", "best_block": -1, "link": ""}
            if n_best >= 0:
                item.update(best=int(page_of[n_best]), best_block=found[n_best]["block"])
            if s_hub >= minimum:
                item["state"] = "hub"
            elif n_best >= 0 and s_best >= minimum:
                item["state"] = "linked" if norm_url(pages[item["best"]]["url"]) in linked else "unlinked"
            else:
                item["state"] = "gap"
            items.append(item)
    return items


def fanout_candidates(items: list[dict], existing: set[tuple[str, str]], site: dict) -> list[dict]:
    """Link opportunities → one candidate per (hub, target): the sub-query it answers best, the hub
    passage closest to it; at most PER_HUB per hub, best first."""
    pages = site["pages"]
    best: dict[tuple[int, int], dict] = {}
    for it in items:
        if it["state"] != "unlinked":
            continue
        if it["hub_block"] < 0:
            it["link"] = "no passage on the hub"
            continue
        if (pages[it["hub"]]["url"], pages[it["best"]]["url"]) in existing:
            it["link"] = "phrase match"
            continue
        key = (it["hub"], it["best"])
        if key not in best or it["similarity"] > best[key]["similarity"]:
            best[key] = it
    per_hub = Counter()
    out = []
    for it in sorted(best.values(), key=lambda x: -x["similarity"]):
        if per_hub[it["hub"]] >= PER_HUB:
            it["link"] = "per-hub limit"
            continue
        per_hub[it["hub"]] += 1
        out.append({"i": it["hub"], "j": it["best"], "b": it["hub_block"], "query": it["query"],
                    "sim": it["similarity"], "kind": "fanout", "item": it})
    return out


def semantic_candidates(site: dict, found: list[dict], pmat: np.ndarray, ok: np.ndarray, cfg: dict,
                        existing: set[tuple[str, str]], ddir: Path, workers: int) -> tuple[list[dict], float]:
    """Each target's name as a query → the closest passages on pages that may link to it and do not."""
    from .pipeline import already_linked

    pages, titles = site["pages"], site["titles"]
    targets = [j for j in range(len(pages)) if ok[j]]
    names = [titles[j] + (f" - {pages[j]['h1']}" if pages[j]["h1"] and pages[j]["h1"] != titles[j] else "")
             for j in targets]
    qmat, cost = pas.embed_queries(names, ddir, workers, pas.TOPIC_TASK)
    page_of = np.array([x["page"] for x in found], dtype=int)
    sources = np.zeros(len(pages), dtype=bool)
    sources[site["sources"]] = True
    linked = [already_linked(p, site["sitewide"]) if sources[i] else set() for i, p in enumerate(pages)]
    out = []
    for qv, j in zip(qmat, targets):
        u = norm_url(pages[j]["url"])
        allowed = sources.copy()
        for i in np.flatnonzero(sources):
            if i == j or pages[i]["lang"] != pages[j]["lang"] or u in linked[i] \
                    or (pages[i]["url"], pages[j]["url"]) in existing:
                allowed[i] = False
        for n, s in pas.best_per_page(pmat @ qv, page_of, allowed[page_of], SEMANTIC_PER_TARGET, cfg["semantic_min"]):
            out.append({"i": int(page_of[n]), "j": j, "b": found[n]["block"], "query": "", "sim": s, "kind": "semantic"})
    return out, cost


def pick_anchors(cands: list[dict], site: dict, ddir: Path, workers: int) -> float:
    """Jev picks each candidate's link text among its passage's phrases (cached in
    jev_anchors.jsonl); sets c["phrase"] - None when no phrase fits - and returns the cost."""
    pages, titles = site["pages"], site["titles"]
    path = ddir / "jev_anchors.jsonl"
    cache = {r["key"]: r["response"] for r in read_jsonl(path)}
    version = jev_version()
    asks = {}
    for c in cands:
        src, tgt = pages[c["i"]], pages[c["j"]]
        hint = " ".join((c["query"], titles[c["j"]], tgt["h1"]))
        c["phrases"] = pas.phrase_candidates(src["blocks"][c["b"]]["text"], src["lang"], hint)
        c["phrase"] = None
        if not c["phrases"]:
            continue
        state = pairs.anchor_state(src, titles[c["i"]], src["blocks"], c["b"], tgt, titles[c["j"]],
                                   c["query"] or titles[c["j"]], src["lang"])
        qs = pairs.anchor_questions(c["phrases"], src["lang"])
        c["key"] = pairs.anchor_cache_key(state, qs, version)
        if c["key"] not in cache or not pairs.anchor_well_formed(cache[c["key"]], c["phrases"]):
            asks[c["key"]] = (state, qs, c["phrases"])

    def ask(x: tuple) -> dict:
        resp = pairs.choose_anchor(x[0], x[1])
        if not pairs.anchor_well_formed(resp, x[2]):
            raise JevError(f"unexpected answer format: {json.dumps(resp)[:200]}")
        return resp

    cost = 0.0
    if asks:
        jev_endpoint()  # fail fast with a clear message when no key is set
        print(f"choosing link text with Jev for {len(asks)} passages ({len(cands) - len(asks)} cached or without a phrase)...")
        for k, resp in call_all(ask, asks, workers, "Jev link text", "passages"):
            append_jsonl(path, {"key": k, "response": resp})
            cache[k] = resp
            cost += resp.get("usage", {}).get("cost", 0) or 0
    for c in cands:
        if c.get("key") in cache:
            c["phrase"], c["phrase_p"] = pairs.chosen_anchor(cache[c["key"]], c["phrases"])
    return cost


def to_rows(cands: list[dict], site: dict) -> list[dict]:
    """Candidates with a link text → rows like the phrase-matched ones (pipeline.make_rows)."""
    from .pipeline import make_rows

    keep = [c for c in cands if c["phrase"]]
    rows = make_rows(site, {(c["i"], c["j"]): (c["phrase"], c["kind"], c["b"]) for c in keep})
    for r, c in zip(rows, keep):
        r["passage_similarity"], r["subquery"] = c["sim"], c["query"]
    return rows


# ---- the whole step ---------------------------------------------------------------------------

def run(pages: list[dict], cfg: dict, args, ddir: Path, domain: str, existing: set[tuple[str, str]]) -> tuple[list[dict], dict, float]:
    """Fan-out (--fanout) and semantic (--semantic) candidates for `run`: rows to judge, what the
    results need (coverage items, hubs) and the cost of the model, the embeddings and the link-text calls."""
    site, cands, items, hubs, gsc_n, cost = candidates(pages, cfg, args, ddir, domain, existing)
    cost += pick_anchors(cands, site, ddir, args.workers)
    for c in cands:
        if c["kind"] == "fanout" and not c["phrase"]:
            c["item"]["link"] = "no phrase in the passage" if not c["phrases"] else "no fitting link text"
    rows = to_rows(cands, site)
    states = Counter(it["state"] for it in items)
    if args.fanout:
        print(f"fan-out: {len(hubs)} hubs, {len(items)} sub-queries ({gsc_n} from GSC) - the hub answers {states['hub']}, "
              f"a linked page {states['linked']}, a page without a link {states['unlinked']}, nobody {states['gap']}")
    print(f"{'fan-out and semantic' if args.fanout and args.semantic else 'fan-out' if args.fanout else 'semantic'} "
          f"pairs to judge: {len(rows)} (no fitting link text: {sum(not c['phrase'] for c in cands)})")
    return rows, {"site": site, "items": items, "hubs": hubs, "model": model() if args.fanout else ""}, cost


def candidates(pages: list[dict], cfg: dict, args, ddir: Path, domain: str,
               existing: set[tuple[str, str]]) -> tuple[dict, list[dict], list[dict], list[int], int, float]:
    """Everything before the link text: (site, candidates, coverage items, hubs, GSC queries used, cost).
    A candidate is {i: source, j: target, b: block, query, sim, kind}."""
    from .pipeline import load_url_list, prepare

    site = prepare(pages, cfg, args.sources, args.targets, load_url_list(args.sources_file, domain),
                   load_url_list(args.targets_file, domain))
    pages, workers = site["pages"], args.workers
    ok = target_ok(site, args.targets, load_url_list(args.targets_file, domain))
    found = pas.passages(pages)
    pmat, cost = pas.embed_passages(found, ddir, min(workers, 4))
    cands, items, hubs, gsc_n = [], [], [], 0
    if args.fanout:
        hubs = pick_hubs(site, args.fanout_pages, load_url_list(args.fanout_pages_file, domain),
                         args.fanout_max)
        gsc = load_gsc(args.gsc, domain) if args.gsc else {}
        generated, model_cost = subqueries(hubs, site, ddir, workers)
        cost += model_cost
        mine = {h: gsc.get(norm_url(pages[h]["url"]), [])[:GSC_PER_HUB] for h in hubs}
        gsc_n = sum(len(v) for v in mine.values())
        all_q = list(dict.fromkeys([q["query"] for h in hubs for q in mine[h] + generated.get(h, [])]))
        qmat, emb_cost = pas.embed_queries(all_q, ddir, min(workers, 4))
        cost += emb_cost
        qvec = dict(zip(all_q, qmat))
        queries = {h: merge(mine[h], generated.get(h, []), qvec, cfg["fanout_merge_min"]) for h in hubs}
        items = coverage(site, hubs, queries, qvec, found, pmat, ok, cfg)
        cands += fanout_candidates(items, existing, site)
    if args.semantic:
        sem, emb_cost = semantic_candidates(site, found, pmat, ok, cfg,
                                            existing | {(pages[c["i"]]["url"], pages[c["j"]]["url"]) for c in cands},
                                            ddir, min(workers, 4))
        cands += sem
        cost += emb_cost
    return site, cands, items, hubs, gsc_n, cost


def dry_run(pages: list[dict], cfg: dict, args, ddir: Path, domain: str) -> dict:
    """What --fanout / --semantic would send, without calling anything."""
    from .pipeline import COST_PER_PAIR, load_url_list, prepare

    site = prepare(pages, cfg, args.sources, args.targets, load_url_list(args.sources_file, domain),
                   load_url_list(args.targets_file, domain))
    found = pas.passages(site["pages"])
    new_passages = pas.count_new(found, [], ddir)
    calls, jev_max = 0, 0
    if args.fanout:
        hubs = pick_hubs(site, args.fanout_pages, load_url_list(args.fanout_pages_file, domain),
                         args.fanout_max)
        if args.gsc:
            load_gsc(args.gsc, domain)  # a bad file shows up now, not after paying
        cached = {r["key"] for r in read_jsonl(ddir / "fanout.jsonl")}
        calls = sum(cache_key(model_input(site["pages"][h], site["titles"][h], site["pages"][h]["lang"]), model())
                    not in cached for h in hubs)
        jev_max += len(hubs) * PER_HUB * 2
        print(f"fan-out: {len(hubs)} hubs, {calls} to ask {model()} (the rest is cached), "
              f"estimated ${calls * COST_PER_HUB:.3f}")
    if args.semantic:
        ok = target_ok(site, args.targets, load_url_list(args.targets_file, domain))
        jev_max += int(ok.sum()) * SEMANTIC_PER_TARGET * 2
    print(f"passages to embed: {new_passages} of {len(found)} (estimated ${new_passages * COST_PER_PASSAGE:.3f}); "
          f"Jev: at most {jev_max} more calls - link text and judgement, 2 per pair, ${jev_max * COST_PER_PAIR:.3f} at most")
    return {"hubs to ask": calls, "passages to embed": new_passages, "Jev calls at most": jev_max}


def write_results(ddir: Path, fan: dict, rows: list[dict]) -> dict:
    """fanout.csv (every sub-query), fanout_gaps.csv (nobody answers) and fanout.json (for the
    report and the panel), with what became of each link opportunity."""
    site, items = fan["site"], fan["items"]
    pages, titles = site["pages"], site["titles"]
    decided = {(r["source_url"], r["target_url"]): r for r in rows if r.get("anchor_type") == "fanout"}
    out = []
    for it in items:
        hub, best = pages[it["hub"]], pages[it["best"]] if it["best"] >= 0 else None
        if it["state"] == "unlinked" and not it["link"]:
            r = decided.get((hub["url"], best["url"]))
            it["link"] = ("" if r is None else "accepted" if r.get("decision") == "accept"
                          else f"rejected: {r.get('decision_reason', '')}")
        out.append({"hub_url": hub["url"], "hub_title": titles[it["hub"]], "subquery": it["query"], "type": it["type"],
                    "source": it["source"], "clicks": it["clicks"], "impressions": it["impressions"],
                    "state": it["state"], "best_url": best["url"] if best else "",
                    "best_title": titles[it["best"]] if best else "", "similarity": it["similarity"],
                    "hub_similarity": it["hub_similarity"],
                    "best_passage": snippet(best, it["best_block"]) if best else "", "link": it["link"]})
    write_csv(ddir / "fanout.csv", out, FIELDS)
    write_csv(ddir / "fanout_gaps.csv", [r for r in out if r["state"] == "gap"], GAP_FIELDS)
    data = {"model": fan["model"], "hubs": [{"u": pages[h]["url"], "t": titles[h]} for h in fan["hubs"]],
            "rows": [{"h": r["hub_url"], "q": r["subquery"], "ty": r["type"], "src": r["source"], "c": r["clicks"],
                      "i": r["impressions"], "st": r["state"], "b": r["best_url"], "bt": r["best_title"],
                      "s": r["similarity"], "hs": r["hub_similarity"], "p": r["best_passage"], "lk": r["link"]}
                     for r in out]}
    (ddir / "fanout.json").write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    return data


def load(ddir: Path) -> dict | None:
    """fanout.json of the last run with --fanout, if any."""
    path = ddir / "fanout.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
