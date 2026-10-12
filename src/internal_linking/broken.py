"""Broken internal links: links in the content to pages of the site that do not load, and the page
each was most likely meant for.

A link in a page's content to an address of the same site that is not among the crawled pages is
checked with one request: 404 or 410 is broken, and so is a "soft 404" - a 200 whose title says the
page was not found ("Wpis nie znaleziony", "Page not found"), which many CMSs serve for a missing
post. A redirect to a working page or any other answer is left alone. The answer is cached in
data/<domain>/link_status.jsonl for a week. For a broken link the tool picks the crawled page whose
title, H1 or slug best matches the link text and the old address (an old slug usually still names
the topic), so the fix is to change the address, not to add a new link - and a new suggestion for
the same pair is dropped.

Results: broken_links.csv, the report's "Broken links" tab and a section in links-for-agent.md.
"""
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .anchors import content_words, same_word
from .common import append_jsonl, norm_url, now, read_jsonl, write_csv
from .crawl import session
from .jev.client import short_error

FILES = re.compile(r"\.(pdf|jpe?g|png|gif|webp|avif|svg|zip|rar|docx?|xlsx?|pptx?|mp4|mp3|webm|xml|txt)$", re.I)
BROKEN = {404, 410}
NOT_FOUND = re.compile(r"<title[^>]*>[^<]*(\b404\b|not found|nie znalezion|nie istnieje|nie ma takiej strony|page does not exist)",
                       re.I)
MAX_AGE = timedelta(days=7)
CACHE = "link_status.jsonl"
MIN_COVER = 0.5   # share of the target's name words the link text and old address must contain
FIELDS = ["source_url", "source_title", "anchor", "broken_url", "status", "suggested_url", "suggested_title", "context"]


def unknown_links(pages: list[dict]) -> list[dict]:
    """Links in content blocks to addresses of the site that are not crawled pages (files excluded)."""
    crawled = {norm_url(p["url"]) for p in pages}
    out = []
    for i, p in enumerate(pages):
        for b, block in enumerate(p.get("blocks", [])):
            for target, text in block.get("anchors", []):
                if target not in crawled and not FILES.search(target.rstrip("/")):
                    out.append({"page": i, "block": b, "href": target, "anchor": text})
    return out


def statuses(urls: set[str], ddir: Path, delay: float = 0.5) -> dict[str, dict]:
    """Address → {status, final}: from the cache when checked within a week, else one GET each."""
    cache = {}
    for r in read_jsonl(ddir / CACHE):
        cache[r["url"]] = r
    fresh = datetime.now().astimezone() - MAX_AGE
    todo = sorted(u for u in urls if u not in cache or datetime.fromisoformat(cache[u]["checked"]) < fresh)
    if todo:
        print(f"checking {len(todo)} links to pages outside the crawl ({len(urls) - len(todo)} checked this week)...")
    for u in todo:
        try:
            r = session.get(f"https://{u}", timeout=20, allow_redirects=True)
            soft = r.status_code == 200 and bool(NOT_FOUND.search(r.text[:20000]))
            row = {"url": u, "status": "soft 404" if soft else r.status_code, "final": norm_url(r.url), "checked": now()}
        except requests.RequestException as e:
            print(f"  ! {u}: {short_error(e)}")
            continue
        append_jsonl(ddir / CACHE, row)
        cache[u] = row
        time.sleep(delay)
    return {u: cache[u] for u in urls if u in cache}


def name_words(text: str, lang: str) -> list[str]:
    return content_words(text.replace("-", " ").replace("/", " "), lang)


def suggest(link_text: str, href: str, pages: list[dict], titles: list[str], lang: str,
            skip: set[int] = frozenset()) -> int | None:
    """The crawled page whose title, H1 or slug is best covered by the link text and the old
    address's last segment; None when no page has at least MIN_COVER of its name words there."""
    clue = name_words(link_text + " " + href.rstrip("/").rsplit("/", 1)[-1], lang)
    best, best_key = None, None
    for j, p in enumerate(pages):
        if p.get("lang") != lang or j in skip:
            continue
        slug = p["url"].rstrip("/").rsplit("/", 1)[-1]
        for name in (titles[j], p.get("h1", ""), slug):
            words = name_words(name, lang)
            if not words:
                continue
            cover = sum(any(same_word(w, c) for c in clue) for w in words) / len(words)
            key = (cover, len(words))
            if cover >= MIN_COVER and (best_key is None or key > best_key):
                best, best_key = j, key
    return best


def find(site: dict, ddir: Path, delay: float = 0.5) -> list[dict]:
    """Broken links in the content of the crawled pages, each with its likely intended page."""
    from .jev.pairs import context

    pages, titles = site["pages"], site["titles"]
    links = unknown_links(pages)
    if not links:
        return []
    status = statuses({x["href"] for x in links}, ddir, delay)
    crawled = {norm_url(p["url"]) for p in pages}
    out = []
    for x in links:
        s = status.get(x["href"])
        if not s or (s["status"] not in BROKEN and s["status"] != "soft 404") or s["final"] in crawled:
            continue
        src = pages[x["page"]]
        j = suggest(x["anchor"], x["href"], pages, titles, src["lang"], site["archives"])
        out.append({"source_url": src["url"], "source_title": titles[x["page"]], "anchor": x["anchor"],
                    "broken_url": f"https://{x['href']}", "status": s["status"],
                    "suggested_url": pages[j]["url"] if j is not None else "",
                    "suggested_title": titles[j] if j is not None else "",
                    "context": context(src["blocks"][x["block"]], x["anchor"])})
    return out


def write(ddir: Path, broken: list[dict]) -> None:
    write_csv(ddir / "broken_links.csv", broken, FIELDS)


def load(ddir: Path) -> list[dict]:
    from .common import read_csv

    path = ddir / "broken_links.csv"
    return read_csv(path) if path.exists() else []
