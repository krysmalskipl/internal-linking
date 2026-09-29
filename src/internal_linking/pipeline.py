"""The unattended run for one or more domains.

crawl the sitemap → exact/partial phrases in paragraphs and lists (anchors.py) → deterministic
rules → Jev judges every pair (jev/pairs.py, cached) → thresholds from config.json → at most N
links per page → data/<domain>/links.csv, recommendations.csv and report.html.

Two modes: automatic (targets and phrases come from the pages' titles) and keyword mode
(--keywords: only the phrases you list, see keywords.py).
"""
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import keywords as kw
from .anchors import AnchorFinder, content_words, detect_lang
from .common import append_jsonl, data_dir, norm_url, read_jsonl, write_csv
from .config import load_config
from .crawl import BOILERPLATE_SHARE, crawl, strip_boilerplate
from .jev import pairs
from .jev.client import JevError, api_key
from .report import write_report

COST_PER_PAIR = 0.00007    # estimate for --dry-run (about 600 input tokens per pair)
FIELDS = ["decision", "decision_reason", "lang", "source_url", "source_title", "placement", "anchor", "anchor_type",
          "target_url", "target_title", "in_menu", "score", "jev_context", "jev_anchor", "jev_value",
          "jev_cannibalisation", "jev_verdict", "context"]


class Progress:
    """Prints "label: done/total unit" about every 10% and at the end, so long steps never look stuck."""

    def __init__(self, label: str, total: int, unit: str):
        self.label, self.total, self.unit, self.done = label, total, unit, 0
        self.every = max(1, total // 10)

    def step(self) -> None:
        self.done += 1
        if self.done % self.every == 0 or self.done == self.total:
            print(f"  {self.label}: {self.done}/{self.total} {self.unit}")


def site_suffixes(pages: list[dict]) -> set[str]:
    """Title endings repeated across many pages (e.g. ' - Site name')."""
    c = Counter()
    for p in pages:
        m = re.search(r"\s[-–|]\s[^-–|]+$", p["title"])
        if m:
            c[m.group(0)] += 1
    return {s for s, n in c.items() if n >= max(3, 0.1 * len(pages))}


def clean_title(title: str, suffixes: set[str]) -> str:
    for s in suffixes:
        if title.endswith(s):
            return title[: -len(s)].strip()
    return title


def sitewide_links(pages: list[dict]) -> dict[str, set[str]]:
    """Per language version: links outside the content repeated on many of its pages - menu, footer,
    sidebars and other template blocks (same share as the crawler's template detection)."""
    by_lang: dict[str, list[dict]] = {}
    for p in pages:
        by_lang.setdefault(p.get("lang", ""), []).append(p)
    out = {}
    for lang, group in by_lang.items():
        c = Counter(link for p in group for link in set(p.get("other_links", [])))
        out[lang] = {link for link, n in c.items() if n >= max(2, BOILERPLATE_SHARE * len(group))}
    return out


def already_linked(src: dict, sitewide: dict[str, set[str]]) -> set[str]:
    """Targets the page already links to: in content, in breadcrumbs and in blocks specific to this
    page (e.g. related posts). Template links (menu, footer, sidebars) don't count - a content link
    is worth more."""
    local = set(src.get("other_links", [])) - sitewide.get(src.get("lang", ""), set())
    return set(src["content_links"]) | set(src.get("breadcrumb_links", [])) | local


def prepare(pages: list[dict], cfg: dict) -> dict:
    """Shared per-site data for both modes. Pages in unsupported languages are left out."""
    strip_boilerplate(pages)  # idempotent; also cleans pages.jsonl saved by an older version
    for p in pages:
        p["lang"] = detect_lang(p)
    skipped_lang = sum(not p["lang"] for p in pages)
    pages = [p for p in pages if p["lang"]]
    suffixes = site_suffixes(pages)
    return {
        "pages": pages,
        "titles": [clean_title(p["title"], suffixes) for p in pages],
        "sitewide": sitewide_links(pages),
        "finder": AnchorFinder(pages),
        "sources": [i for i, p in enumerate(pages) if len(p["text"].split()) >= cfg["min_words"]],
        "skipped_lang": skipped_lang,
    }


def drop_template_phrases(anchors: dict, cfg: dict, n_sources: int) -> int:
    """The same phrase to the same target on many pages is a template element (author byline, post
    footer, repeated promo block), not content - not a link candidate. Returns how many were dropped."""
    repeats = Counter((found[0].lower(), j) for (i, j), found in anchors.items())
    limit = max(3, cfg["template_share"] * n_sources)
    template = [k for k, found in anchors.items() if repeats[(found[0].lower(), k[1])] > limit]
    for k in template:
        del anchors[k]
    return len(template)


def make_rows(site: dict, anchors: dict, keyword_of: dict | None = None) -> list[dict]:
    pages, titles = site["pages"], site["titles"]
    rows = []
    for (i, j), (phrase, kind, b) in anchors.items():
        blocks, lang = pages[i]["blocks"], pages[i]["lang"]
        row = {
            "lang": lang,
            "source_url": pages[i]["url"], "source_title": titles[i],
            "target_url": pages[j]["url"], "target_title": titles[j],
            "anchor": phrase, "anchor_type": kind, "placement": pairs.PLACEMENT.get(blocks[b]["tag"], "other"),
            "context": pairs.context(blocks[b], phrase),
            "in_menu": int(norm_url(pages[j]["url"]) in site["sitewide"].get(lang, set())),
            "_state": pairs.build_state(pages[i], titles[i], blocks, b, pages[j], titles[j], phrase, lang),
        }
        if keyword_of is not None:
            row["keyword"] = keyword_of[(i, j)]
        rows.append(row)
    return rows


def find_candidates(pages: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    """Automatic mode: targets and phrases come from the pages' own titles, H1s and slugs."""
    site = prepare(pages, cfg)
    pages, titles, finder, sources = site["pages"], site["titles"], site["finder"], site["sources"]

    anchors = {}  # (source, target) -> (phrase, exact/partial, block index)
    progress = Progress("searching phrases", len(sources), "pages")
    for i in sources:
        progress.step()
        linked = already_linked(pages[i], site["sitewide"])
        for j, t in enumerate(pages):
            # links only within one language (/en/ versions etc. are separate)
            if j != i and norm_url(t["url"]) not in linked and t["lang"] == pages[i]["lang"]:
                found = finder.find_in_blocks(pages[i].get("blocks", []), titles[i], titles[j], t["h1"], t["url"],
                                              pages[i]["lang"])
                if found:
                    anchors[(i, j)] = found

    template = drop_template_phrases(anchors, cfg, len(sources))
    # a phrase that is the exact name of another page is reserved for that page
    reserved = 0
    for (i, j), (phrase, kind, _) in list(anchors.items()):
        if kind == "partial" and any(
                k != j and t["lang"] == pages[i]["lang"]
                and (m := finder.find(phrase, "", titles[k], t["h1"], t["url"], t["lang"])) and m[1] == "exact"
                for k, t in enumerate(pages)):
            del anchors[(i, j)]
            reserved += 1

    info = {"languages": dict(Counter(p["lang"] for p in pages)), "sources": len(sources),
            "template phrases": template, "reserved phrases": reserved}
    if site["skipped_lang"]:
        info["pages in unsupported languages"] = site["skipped_lang"]
    return make_rows(site, anchors), info


def keyword_candidates(pages: list[dict], cfg: dict, keywords: list[dict]) -> tuple[list[dict], dict, list[dict]]:
    """Keyword mode: only the given phrases, each linked to its (given or auto-picked) target."""
    site = prepare(pages, cfg)
    pages, titles, finder, sources = site["pages"], site["titles"], site["finder"], site["sources"]
    resolved, warnings = kw.resolve(keywords, pages, titles)
    for w in warnings:
        print(f"  ! {w}")

    anchors, keyword_of = {}, {}
    progress = Progress("searching keywords", len(resolved), "keywords")
    for k in resolved:
        progress.step()
        j, target = k["target"], pages[k["target"]]
        n_words = len(content_words(k["keyword"], target["lang"]))
        for i in sources:
            if i == j or pages[i]["lang"] != target["lang"] or (i, j) in anchors:
                continue
            if norm_url(target["url"]) in already_linked(pages[i], site["sitewide"]):
                continue
            found = finder.find_in_blocks(pages[i].get("blocks", []), titles[i], k["keyword"], "", "",
                                          target["lang"], min_hits=min(2, n_words))
            if found and (k["match"] == "partial" or found[1] == "exact"):
                anchors[(i, j)] = found
                keyword_of[(i, j)] = k["keyword"]
    # no template-phrase rule here: the keywords are chosen on purpose, and template blocks are
    # already stripped by the crawler, so a keyword repeated across many pages is a real use
    info = {"keywords": len(keywords), "resolved": len(resolved), "sources": len(sources)}
    return make_rows(site, anchors, keyword_of), info, resolved


def keyword_summary(resolved: list[dict], rows: list[dict], pages: list[dict]) -> list[dict]:
    out = []
    for k in resolved:
        mine = [r for r in rows if r.get("keyword") == k["keyword"]]
        reasons = Counter(r["decision_reason"] for r in mine if r["decision"] == "reject")
        out.append({
            "keyword": k["keyword"], "match": k["match"], "target_url": k["target_url"],
            "target_auto_picked": int(k["auto_target"]), "pages_with_phrase": len(mine),
            "links_to_insert": sum(r["decision"] == "accept" for r in mine),
            "top_reject_reasons": ", ".join(f"{r} {n}" for r, n in reasons.most_common(3)),
        })
    return out


def judge(rows: list[dict], ddir: Path, workers: int) -> float:
    """Judges every pair with its own Jev call, cached in jev_pairs.jsonl."""
    cache_path = ddir / "jev_pairs.jsonl"
    cache = {r["key"]: r["response"] for r in read_jsonl(cache_path)}
    keys = [pairs.cache_key(r["_state"], r["lang"]) for r in rows]
    todo = {k: (r["_state"], r["lang"]) for k, r in zip(keys, rows) if k not in cache}
    cost, failed = 0.0, 0
    if todo:
        api_key()  # fail fast with a clear message when the key is missing
        print(f"judging {len(todo)} pairs with Jev ({len(rows) - len(todo)} cached)...")
    progress = Progress("judged", len(todo), "pairs")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {k: ex.submit(pairs.judge, st, lang) for k, (st, lang) in todo.items()}
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
            progress.step()
    if todo and failed > 0.2 * len(todo):
        raise RuntimeError(f"Jev failed on {failed} of {len(todo)} pairs - stopping this domain")
    for r, k in zip(rows, keys):
        if k in cache:
            r.update(pairs.summarize(cache[k], r["lang"]))
    print(f"Jev: {len(todo) - failed} new pairs, {len(rows) - len(todo)} cached, cost ${cost:.4f}")
    return cost


def select(rows: list[dict], cfg: dict, max_links: int) -> None:
    """Sets decision (accept/reject) and decision_reason on every pair."""
    for r in rows:
        r["decision_reason"] = pairs.reject_reason(r, cfg) if "score" in r else "not_judged"
    passing = sorted((r for r in rows if not r["decision_reason"]), key=lambda r: -r["score"])
    # one phrase in the text carries one link; then the per-page link limit
    used_phrases, per_page = set(), Counter()
    for r in passing:
        key = (r["source_url"], r["anchor"].lower())
        if key in used_phrases:
            r["decision_reason"] = "duplicate_phrase"
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
    started = time.monotonic()
    if not args.no_crawl:
        crawl(domain, args.sitemap, args.delay)
    pages = read_jsonl(ddir / "pages.jsonl")
    if not pages or "blocks" not in pages[0]:
        raise RuntimeError("no up-to-date pages.jsonl - run without --no-crawl")

    resolved = None
    if args.keywords:
        rows, info, resolved = keyword_candidates(pages, cfg, kw.load_keywords(args.keywords))
    else:
        rows, info = find_candidates(pages, cfg)
    print(f"pages: {len(pages)}, " + ", ".join(f"{k}: {v}" for k, v in info.items())
          + f", pairs with a phrase: {len(rows)}")
    if args.dry_run:
        cache = {r["key"] for r in read_jsonl(ddir / "jev_pairs.jsonl")}
        new = sum(pairs.cache_key(r["_state"], r["lang"]) not in cache for r in rows)
        print(f"to judge with Jev: {new} (the rest is cached), estimated cost ${new * COST_PER_PAIR:.3f}")
        return {"domain": domain, "pairs": len(rows), "to judge": new}

    cost = judge(rows, ddir, args.workers) if rows else 0.0
    select(rows, cfg, args.max_links or cfg["max_links_per_page"])
    accepted = [r for r in rows if r["decision"] == "accept"]
    rejected = [r for r in rows if r["decision"] == "reject"]

    rows.sort(key=lambda r: (r["decision"] != "accept", r["source_url"], -r.get("score", 0)))
    fields = FIELDS + (["keyword"] if resolved is not None else [])
    write_csv(ddir / "recommendations.csv", rows, fields)
    write_csv(ddir / "links.csv", accepted, fields[2:])
    summary = None
    if resolved is not None:
        summary = keyword_summary(resolved, rows, pages)
        write_csv(ddir / "keywords_summary.csv", summary, list(summary[0].keys()) if summary else ["keyword"])
    pages_with = len({r["source_url"] for r in accepted})
    write_report(ddir / "report.html", domain, accepted, rejected, {
        "links to insert": len(accepted), "pages with new links": pages_with,
        "suggestions judged": len(rows), "Jev cost": f"${cost:.3f}"}, keyword_summary=summary)
    print(f"links to insert: {len(accepted)} on {pages_with} pages "
          f"(rejected: {dict(Counter(r['decision_reason'] for r in rejected))})")
    print(f"done in {time.monotonic() - started:.0f} s")
    print(f"→ {ddir / 'report.html'}\n→ {ddir / 'links.csv'}")
    return {"domain": domain, "links": len(accepted), "pages": pages_with, "cost": round(cost, 4)}


def run(args) -> None:
    domains = list(args.domain)
    if args.domains_file:
        domains += [ln.strip() for ln in Path(args.domains_file).read_text().splitlines()
                    if ln.strip() and not ln.startswith("#")]
    if not domains:
        raise SystemExit("give --domain or --domains-file")
    if args.sitemap and len(domains) > 1:
        raise SystemExit("--sitemap works with a single domain only")

    cfg = load_config(args.config)
    summary = []
    for d in dict.fromkeys(domains):
        try:
            summary.append(process(d, args, cfg))
        except RuntimeError as e:
            print(f"! {d}: {e}")
            summary.append({"domain": d, "error": str(e)[:120]})
    if len(summary) > 1:
        print("\n=== summary")
        for s in summary:
            print("  " + ", ".join(f"{k}: {v}" for k, v in s.items()))


def add_parser(sub) -> None:
    p = sub.add_parser("run", help="crawl, judge and report internal links for one or more domains")
    p.add_argument("--domain", action="append", default=[], help="domain (repeatable)")
    p.add_argument("--domains-file", help="file with one domain per line (# for comments)")
    p.add_argument("--keywords", help="keyword mode: CSV/TXT with the phrases to link (see keywords.py)")
    p.add_argument("--max-links", type=int, help="max new links per page (default from config)")
    p.add_argument("--no-crawl", action="store_true", help="reuse the saved pages.jsonl")
    p.add_argument("--dry-run", action="store_true", help="count pairs and estimate cost without calling Jev")
    p.add_argument("--sitemap", help="sitemap URL (single domain only)")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between page downloads")
    p.add_argument("--workers", type=int, default=4, help="parallel Jev calls")
    p.set_defaults(func=run)
