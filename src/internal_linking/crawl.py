"""Sitemap crawl → data/<domain>/pages.jsonl.

For every page in the sitemap: title, H1, meta description, language, the main content as text,
typed content blocks (paragraph, list item, heading...) and internal links - in the content,
in breadcrumbs and everywhere else (menu, footer, sidebar). Template blocks repeated across pages
are then removed from the content (strip_boilerplate), so any theme works without configuration,
and so are excerpts of other pages on listings - tag, category and blog index pages (strip_excerpts).
Respects robots.txt Disallow rules and Crawl-delay for the "internal-linking" user agent.
"""
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from urllib import robotparser
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .common import data_dir, is_internal, norm_url, write_jsonl

ROBOTS_AGENT = "internal-linking"
UA = f"Mozilla/5.0 (compatible; {ROBOTS_AGENT}/0.1; +https://github.com/krysmalskipl/internal-linking)"
NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
LINK_MARK = "¶"  # boundary in block text: an existing link, code, or the end of a block
BLOCKS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "dt", "dd", "blockquote", "figcaption"]
BOILERPLATE = ["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg"]
BREADCRUMB_HINTS = ("breadcrumb", "okruszk")
BOILERPLATE_SHARE = 0.3  # a block repeated on this share of pages (min. 3) is part of the template...
BOILERPLATE_PAGES = 5    # ...and so is one repeated verbatim on this many pages
EXCERPT_WORDS = 8        # a block this long that repeats another page's description or text is an excerpt
TRUNCATED = re.compile(r"\s*(?:…|\.\.\.|\[(?:…|\.\.\.)\]|czytaj dalej|czytaj więcej|read more|continue reading)\s*$")

session = requests.Session()
session.headers["User-Agent"] = UA


def fetch(url: str) -> requests.Response | None:
    try:
        return session.get(url, timeout=30)
    except requests.RequestException as e:
        print(f"  ! {url}: {e}")
        return None


def load_robots(base: str) -> tuple[robotparser.RobotFileParser, list[str]]:
    """robots.txt rules and the sitemaps it declares; a missing robots.txt allows everything."""
    rp = robotparser.RobotFileParser()
    r = fetch(f"{base}/robots.txt")
    text = r.text if r is not None and r.ok else ""
    rp.parse(text.splitlines())
    sitemaps = [ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.lower().startswith("sitemap:")]
    return rp, sitemaps


def parse_sitemap(xml: bytes) -> ET.Element | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    return root if root.tag in (f"{NS}urlset", f"{NS}sitemapindex") else None


def find_sitemap(base: str, declared: list[str]) -> str:
    candidates = declared + [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml", f"{base}/wp-sitemap.xml"]
    for url in candidates:
        r = fetch(url)
        if r is not None and r.ok and parse_sitemap(r.content) is not None:
            return url
    raise RuntimeError(f"no sitemap found for {base} (tried: {', '.join(candidates)})")


def sitemap_urls(url: str, seen: set[str] | None = None) -> list[str]:
    seen = seen if seen is not None else set()
    if url in seen:
        return []
    seen.add(url)
    r = fetch(url)
    root = parse_sitemap(r.content) if r is not None and r.ok else None
    if root is None:
        print(f"  ! skipped sitemap {url}")
        return []
    locs = [el.text.strip() for el in root.iter(f"{NS}loc") if el.text]
    if root.tag == f"{NS}sitemapindex":
        return [u for sub in locs for u in sitemap_urls(sub, seen)]
    return locs


def clean_text(tag) -> str:
    return " ".join(tag.get_text(" ", strip=True).split())


def meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": name})
    return (tag.get("content") or "").strip() if tag else ""


def main_content(soup: BeautifulSoup):
    """<main> (or <body>), unless a single <article> holds most of its text - then that <article>.
    On a listing with several post cards (<article> each) no card dominates, so the whole
    container is the content."""
    body = soup.body or soup
    main = soup.find("main")
    # some themes render an empty <main> with the article next to it
    body_len = len(body.get_text(" ", strip=True))
    container = main if main and len(main.get_text(" ", strip=True)) >= 0.3 * body_len else body
    total = len(container.get_text(" ", strip=True)) or 1
    sizes = sorted(((len(a.get_text(" ", strip=True)), n) for n, a in enumerate(container.find_all("article"))),
                   reverse=True)
    if sizes and sizes[0][0] >= 0.5 * total and (len(sizes) == 1 or sizes[1][0] < 0.3 * sizes[0][0]):
        return container.find_all("article")[sizes[0][1]]
    return container


def is_breadcrumb(tag) -> bool:
    attrs = " ".join([tag.get("aria-label") or "", tag.get("id") or "", tag.get("itemtype") or "",
                      " ".join(tag.get("class") or [])]).lower()
    return any(h in attrs for h in BREADCRUMB_HINTS)


def internal_links(root, url: str, domain: str) -> set[str]:
    links = set()
    for a in root.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if href.startswith("http") and is_internal(href, domain):
            links.add(norm_url(href))
    links.discard(norm_url(url))
    return links


def link_target(a, url: str, domain: str) -> str | None:
    """Normalised internal URL an <a> points to, or None."""
    href = urljoin(url, a.get("href") or "")
    return norm_url(href) if href.startswith("http") and is_internal(href, domain) else None


def extract(url: str, html: str, domain: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.find("link", rel="canonical")
    title = soup.title.get_text(strip=True) if soup.title else ""
    h1 = soup.find("h1")
    h1 = h1.get_text(" ", strip=True) if h1 else ""  # read now - block separators are added below
    html_tag = soup.find("html")
    lang = (html_tag.get("lang") or "").split("-")[0].lower() if html_tag else ""

    # links outside the content are collected before navigation is stripped from the content
    all_links = internal_links(soup, url, domain)
    breadcrumb_links = set()
    for bc in soup.find_all(is_breadcrumb):
        breadcrumb_links |= internal_links(bc, url, domain)

    content = main_content(soup)
    for tag in content.find_all(BOILERPLATE):
        tag.decompose()
    content_links = internal_links(content, url, domain)
    text = clean_text(content)
    # leaf blocks (paragraph, list item, heading...) without nested blocks
    leaves = [b for b in content.find_all(BLOCKS) if not b.find(BLOCKS)]
    raw = [clean_text(b) for b in leaves]
    # a block inside a link (a whole post card wrapped in <a>) is link text itself
    wrapping = [b.find_parent("a", href=True) for b in leaves]
    anchors = [[[t, clean_text(a)] for a in b.find_all("a", href=True) if (t := link_target(a, url, domain))]
               + ([[t, r]] if w is not None and (t := link_target(w, url, domain)) else [])
               for b, w, r in zip(leaves, wrapping, raw)]
    leaf_links = [sorted({t for t, _ in an} - {norm_url(url)}) for an in anchors]
    # existing link text and code become a separator so they are never proposed as a new anchor;
    # a separator at the end of each block keeps a phrase from spanning a heading and a paragraph
    for a in content.find_all(["a", "pre", "code"]):
        a.replace_with(f" {LINK_MARK} ")
    blocks = [{"tag": b.name, "text": LINK_MARK if w is not None else clean_text(b), "raw": r, "links": lk, "anchors": an}
              for b, w, r, lk, an in zip(leaves, wrapping, raw, leaf_links, anchors)]
    for block in content.find_all(BLOCKS):
        block.append(f" {LINK_MARK} ")
    text_free = clean_text(content)
    # text outside blocks (e.g. bare text in a <div>) becomes one "inne" (other) block
    for b in leaves:
        b.extract()
    rest = clean_text(content)
    if len(rest.replace(LINK_MARK, " ").split()) >= 5:
        in_leaves = {link for lk in leaf_links for link in lk}
        blocks.append({"tag": "inne", "text": rest, "raw": rest.replace(f" {LINK_MARK} ", " "),
                       "links": sorted(content_links - in_leaves)})
    blocks = [b for b in blocks if b["text"].replace(LINK_MARK, "").strip()]

    return {
        "url": url,
        "lang": lang,
        "title": title,
        "h1": h1,
        "meta": meta(soup, "description"),
        "robots": meta(soup, "robots").lower(),
        "canonical": urljoin(url, canonical["href"]) if canonical and canonical.get("href") else url,
        "text": text,
        "text_free": text_free,
        "blocks": blocks,
        "content_links": sorted(content_links),
        "breadcrumb_links": sorted(breadcrumb_links),
        "other_links": sorted(all_links - content_links),
    }


def block_key(block: dict) -> str:
    return " ".join(block["raw"].lower().split())


def strip_boilerplate(pages: list[dict], share: float = BOILERPLATE_SHARE) -> int:
    """Removes template blocks - text repeated on many pages of the same language (top bars with a
    phone number, footers, sidebars, "areas we cover" sections) - from each page's blocks, text and
    content links. Works on any theme, because it compares pages instead of guessing from tags.
    Returns the number of blocks removed."""
    by_lang: dict[str, list[dict]] = {}
    for p in pages:
        by_lang.setdefault(p.get("lang", ""), []).append(p)
    removed = 0
    for group in by_lang.values():
        if len(group) < 4:
            continue  # too few pages to tell a template from content
        seen = Counter(k for p in group for k in {block_key(b) for b in p["blocks"]})
        # an identical block on BOILERPLATE_PAGES pages is a template even if it sits in only one
        # section of the site (e.g. an author bio under every blog post)
        limit = max(3, min(share * len(group), BOILERPLATE_PAGES))
        for p in group:
            keep = [b for b in p["blocks"] if seen[block_key(b)] < limit]
            if len(keep) == len(p["blocks"]):
                continue
            removed += len(p["blocks"]) - len(keep)
            dropped_links = {lk for b in p["blocks"] if b not in keep for lk in b.get("links", [])}
            kept_links = {lk for b in keep for lk in b.get("links", [])}
            gone = dropped_links - kept_links
            p["blocks"] = keep
            p["text"] = " ".join(b["raw"] for b in keep)
            p["text_free"] = f" {LINK_MARK} ".join(b["text"] for b in keep)
            p["content_links"] = sorted(set(p["content_links"]) - gone)
            p["other_links"] = sorted(set(p["other_links"]) | gone)
    return removed


def excerpt_key(text: str) -> tuple[str, bool]:
    """Comparison form of a text - lower case, single spaces, no trailing "…" or "read more" - and
    whether such an ending was there (the text was cut short)."""
    key, cut = " ".join(text.lower().split()), False
    while m := TRUNCATED.search(key):
        key, cut = key[:m.start()], True
    return key.strip(" .,;:-–—"), cut


def first_words(key: str) -> str:
    return " ".join(key.split()[:EXCERPT_WORDS])


def strip_excerpts(pages: list[dict], min_words: int = 150) -> int:
    """Removes excerpts of other pages - the post previews on tag, category and blog index pages,
    "related posts" boxes - from each page's content: they belong to the page they preview, so a
    link added there would be added in the wrong place. A block is an excerpt of a page it links to
    when it repeats that page's description or the start of its text and is the description itself,
    is cut short ("…", "read more"), or sits among excerpts of other pages too; a single quote of a
    linked page stays. A page whose content is mostly excerpts is marked as a listing (never a link
    source). Idempotent. Returns the number of blocks removed."""
    keys = {norm_url(p["url"]): (excerpt_key(p.get("meta", ""))[0],
                                  " ".join(excerpt_key(b["raw"])[0] for b in p["blocks"])) for p in pages}
    starts: dict[str, set[str]] = {}  # first words of each description and block → pages
    for p in pages:
        u = norm_url(p["url"])
        for key in [keys[u][0]] + [excerpt_key(b["raw"])[0] for b in p["blocks"]]:
            if len(key.split()) >= EXCERPT_WORDS:
                starts.setdefault(first_words(key), set()).add(u)
    found = {}
    for p in pages:
        u = norm_url(p["url"])
        linked = set(p.get("content_links", [])) | set(p.get("other_links", [])) | set(p.get("breadcrumb_links", []))
        hits = []  # (block, page it previews, previews it on its own)
        for n, b in enumerate(p["blocks"]):
            key, cut = excerpt_key(b["raw"])
            if len(key.split()) < EXCERPT_WORDS:
                continue
            for q in (starts.get(first_words(key), set()) & linked) - {u}:
                meta, text = keys[q]
                if key == meta or (cut and (meta.startswith(key) or key in text)):
                    hits.append((n, q, True))
                    break
                if key in text or key in meta:
                    hits.append((n, q, False))
                    break
        if len({q for _, q, _ in hits}) >= 2:
            found[u] = {n for n, _, _ in hits}
        elif any(strong for _, _, strong in hits):
            found[u] = {n for n, _, strong in hits if strong}
    removed = 0
    for p in pages:
        drop = found.get(norm_url(p["url"]))
        if not drop:
            continue
        total = sum(len(b["raw"].split()) for b in p["blocks"]) or 1
        cut_words = sum(len(p["blocks"][n]["raw"].split()) for n in drop)
        p["blocks"] = [b for n, b in enumerate(p["blocks"]) if n not in drop]
        p["text"] = " ".join(b["raw"] for b in p["blocks"])
        p["text_free"] = f" {LINK_MARK} ".join(b["text"] for b in p["blocks"])
        if cut_words >= 0.5 * total or (len(drop) >= 3 and total - cut_words < min_words):
            p["listing"] = True
        removed += len(drop)
    return removed


def crawl(domain: str, sitemap: str | None = None, delay: float = 0.5) -> list[dict]:
    """Downloads the sitemap pages and writes data/<domain>/pages.jsonl."""
    base = f"https://{domain.removeprefix('https://').removeprefix('http://').strip('/')}"
    robots, declared = load_robots(base)
    delay = max(delay, float(robots.crawl_delay(ROBOTS_AGENT) or 0))
    sitemap = sitemap or find_sitemap(base, declared)
    print(f"sitemap: {sitemap}")
    urls = list(dict.fromkeys(sitemap_urls(sitemap)))
    print(f"URLs in sitemap: {len(urls)}")

    print(f"downloading {len(urls)} pages (about {len(urls) * (delay + 0.3) / 60:.0f} min)...")
    pages, skipped = [], {}
    for i, url in enumerate(urls, 1):
        if not robots.can_fetch(ROBOTS_AGENT, url):
            skipped[url] = "disallowed by robots.txt"
            continue
        r = fetch(url)
        time.sleep(delay)
        if r is None or r.status_code != 200:
            skipped[url] = f"status {getattr(r, 'status_code', 'error')}"
            continue
        if norm_url(r.url) != norm_url(url):
            skipped[url] = f"redirects to {r.url}"
            continue
        page = extract(url, r.text, domain)
        if "noindex" in page["robots"]:
            skipped[url] = "noindex"
        elif norm_url(page["canonical"]) != norm_url(url):
            skipped[url] = f"canonical → {page['canonical']}"
        else:
            pages.append(page)
            print(f"[{i}/{len(urls)}] {url}  ({len(page['text'].split())} words, links: "
                  f"{len(page['content_links'])} in content, {len(page['breadcrumb_links'])} in breadcrumbs, "
                  f"{len(page['other_links'])} elsewhere)")

    removed = strip_boilerplate(pages)
    if removed:
        print(f"\ntemplate blocks removed from content (repeated across pages): {removed}")
    excerpts = strip_excerpts(pages)
    if excerpts:
        print(f"excerpts of other pages removed from content (listings, related posts): {excerpts}, "
              f"listing pages: {sum(bool(p.get('listing')) for p in pages)}")
    out = data_dir(domain) / "pages.jsonl"
    write_jsonl(out, pages)
    print(f"\nsaved {len(pages)} pages → {out}")
    if skipped:
        print(f"skipped {len(skipped)}:")
        for url, why in skipped.items():
            print(f"  - {url}: {why}")
    return pages


def add_parser(sub) -> None:
    p = sub.add_parser("crawl", help="download the sitemap pages only (run does this too)")
    p.add_argument("--domain", required=True, help="e.g. example.com")
    p.add_argument("--sitemap", help="sitemap URL, if robots.txt does not declare one")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    p.set_defaults(func=lambda args: crawl(args.domain, args.sitemap, args.delay))
