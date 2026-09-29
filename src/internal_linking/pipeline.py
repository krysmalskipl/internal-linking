"""The unattended run for one or more domains.

crawl the sitemap → exact/partial phrases in paragraphs and lists (anchors.py) → deterministic
rules → Jev judges every pair (jev/pairs.py, cached) → thresholds from config.json → at most N
links per page → data/<domain>/links.csv, recommendations.csv and report.html.
"""
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .anchors import AnchorFinder
from .common import append_jsonl, data_dir, norm_url, read_jsonl, write_csv
from .config import load_config
from .crawl import crawl
from .jev import pairs
from .jev.client import JevError
from .report import write_report

COST_PER_PAIR = 0.00007    # estimate for --dry-run (about 600 input tokens per pair)
FIELDS = ["decision", "decision_reason", "source_url", "source_title", "placement", "anchor", "anchor_type",
          "target_url", "target_title", "in_menu", "score", "jev_context", "jev_anchor", "jev_value",
          "jev_cannibalisation", "jev_verdict", "context"]


def site_suffixes(pages: list[dict]) -> set[str]:
    """Title endings repeated across many pages (e.g. ' - Site name')."""
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
    """Links outside the content present on at least half of the pages - menu and footer."""
    c = Counter(link for p in pages for link in p.get("other_links", []))
    return {link for link, n in c.items() if n >= 0.5 * len(pages)}


def already_linked(src: dict, sitewide: set[str]) -> set[str]:
    """Targets the page already links to: in content, in breadcrumbs and in blocks specific to this
    page (e.g. related posts). Menu and footer links don't count - a content link is worth more."""
    local = set(src.get("other_links", [])) - sitewide
    return set(src["content_links"]) | set(src.get("breadcrumb_links", [])) | local


def find_candidates(pages: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    """(source, phrase, target) pairs with the phrase present in the text, after deterministic rules."""
    suffixes = site_suffixes(pages)
    sitewide = sitewide_links(pages)
    finder = AnchorFinder(pages)
    titles = [clean_title(p["title"], suffixes) for p in pages]
    sources = [i for i, p in enumerate(pages) if len(p["text"].split()) >= cfg["min_words"]]

    anchors = {}  # (source, target) -> (phrase, exact/partial, block index)
    for i in sources:
        linked = already_linked(pages[i], sitewide)
        for j, t in enumerate(pages):
            # links only within one language (/en/ versions etc. are separate)
            if j != i and norm_url(t["url"]) not in linked and t.get("lang", "") == pages[i].get("lang", ""):
                found = finder.find_in_blocks(pages[i].get("blocks", []), titles[i], titles[j], t["h1"], t["url"])
                if found:
                    anchors[(i, j)] = found

    # the same phrase to the same target on many pages is a template element (author byline,
    # post footer), not content - not a link candidate
    repeats = Counter((phrase.lower(), j) for (i, j), (phrase, _, _) in anchors.items())
    limit = max(3, cfg["template_share"] * len(sources))
    template = [k for k, (phrase, _, _) in anchors.items() if repeats[(phrase.lower(), k[1])] > limit]
    for k in template:
        del anchors[k]
    # a phrase that is the exact name of another page is reserved for that page
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
        state = pairs.build_state(pages[i], titles[i], block, pages[j], titles[j], phrase)
        rows.append({
            "source_url": pages[i]["url"], "source_title": titles[i],
            "target_url": pages[j]["url"], "target_title": titles[j],
            "anchor": phrase, "anchor_type": kind, "placement": pairs.PLACEMENT.get(block["tag"], "other"),
            "context": state["fragment_z_fraza"], "in_menu": int(norm_url(pages[j]["url"]) in sitewide),
            "_state": state,
        })
    info = {"sources": len(sources), "template phrases": len(template), "reserved phrases": reserved}
    return rows, info


def judge(rows: list[dict], ddir: Path, workers: int) -> float:
    """Judges every pair with its own Jev call, cached in jev_pairs.jsonl."""
    cache_path = ddir / "jev_pairs.jsonl"
    cache = {r["key"]: r["response"] for r in read_jsonl(cache_path)}
    keys = [pairs.cache_key(r["_state"]) for r in rows]
    todo = {k: r["_state"] for k, r in zip(keys, rows) if k not in cache}
    cost, failed = 0.0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {k: ex.submit(pairs.judge, st) for k, st in todo.items()}
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
                print(f"  judged pairs: {n}/{len(todo)}")
    if todo and failed > 0.2 * len(todo):
        raise RuntimeError(f"Jev failed on {failed} of {len(todo)} pairs - stopping this domain")
    for r, k in zip(rows, keys):
        if k in cache:
            r.update(pairs.summarize(cache[k]))
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
    if not args.no_crawl:
        crawl(domain, args.sitemap, args.delay)
    pages = read_jsonl(ddir / "pages.jsonl")
    if not pages or "blocks" not in pages[0]:
        raise RuntimeError("no up-to-date pages.jsonl - run without --no-crawl")

    rows, info = find_candidates(pages, cfg)
    print(f"pages: {len(pages)}, " + ", ".join(f"{k}: {v}" for k, v in info.items())
          + f", pairs with a phrase: {len(rows)}")
    if args.dry_run:
        cache = {r["key"] for r in read_jsonl(ddir / "jev_pairs.jsonl")}
        new = sum(pairs.cache_key(r["_state"]) not in cache for r in rows)
        print(f"to judge with Jev: {new} (the rest is cached), estimated cost ${new * COST_PER_PAIR:.3f}")
        return {"domain": domain, "pairs": len(rows), "to judge": new}

    cost = judge(rows, ddir, args.workers) if rows else 0.0
    select(rows, cfg, args.max_links or cfg["max_links_per_page"])
    accepted = [r for r in rows if r["decision"] == "accept"]
    rejected = [r for r in rows if r["decision"] == "reject"]

    rows.sort(key=lambda r: (r["decision"] != "accept", r["source_url"], -r.get("score", 0)))
    write_csv(ddir / "recommendations.csv", rows, FIELDS)
    write_csv(ddir / "links.csv", accepted, FIELDS[2:])
    pages_with = len({r["source_url"] for r in accepted})
    write_report(ddir / "report.html", domain, accepted, rejected, {
        "links to insert": len(accepted), "pages with new links": pages_with,
        "suggestions judged": len(rows), "Jev cost": f"${cost:.3f}"})
    print(f"links to insert: {len(accepted)} on {pages_with} pages "
          f"(rejected: {dict(Counter(r['decision_reason'] for r in rejected))})")
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
    p.add_argument("--max-links", type=int, help="max new links per page (default from config)")
    p.add_argument("--no-crawl", action="store_true", help="reuse the saved pages.jsonl")
    p.add_argument("--dry-run", action="store_true", help="count pairs and estimate cost without calling Jev")
    p.add_argument("--sitemap", help="sitemap URL (single domain only)")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between page downloads")
    p.add_argument("--workers", type=int, default=4, help="parallel Jev calls")
    p.set_defaults(func=run)
