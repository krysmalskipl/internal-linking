"""Sitemap crawl → data/<domain>/pages.jsonl.

For every page in the sitemap: title, H1, meta description, language, the main content as text,
typed content blocks (paragraph, list item, heading...) and internal links - in the content,
in breadcrumbs and everywhere else (menu, footer, sidebar). Respects robots.txt Disallow rules
and Crawl-delay for the "internal-linking" user agent.
"""
import time
import xml.etree.ElementTree as ET
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
    Avoids picking a post card from a listing as the page content."""
    body = soup.body or soup
    main = soup.find("main")
    # some themes render an empty <main> with the article next to it
    body_len = len(body.get_text(" ", strip=True))
    container = main if main and len(main.get_text(" ", strip=True)) >= 0.3 * body_len else body
    total = len(container.get_text(" ", strip=True)) or 1
    articles = container.find_all("article")
    if articles:
        best = max(articles, key=lambda a: len(a.get_text(" ", strip=True)))
        if len(best.get_text(" ", strip=True)) >= 0.5 * total:
            return best
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
    # existing link text and code become a separator so they are never proposed as a new anchor;
    # a separator at the end of each block keeps a phrase from spanning a heading and a paragraph
    for a in content.find_all(["a", "pre", "code"]):
        a.replace_with(f" {LINK_MARK} ")
    blocks = [{"tag": b.name, "text": clean_text(b), "raw": r} for b, r in zip(leaves, raw)]
    for block in content.find_all(BLOCKS):
        block.append(f" {LINK_MARK} ")
    text_free = clean_text(content)
    # text outside blocks (e.g. bare text in a <div>) becomes one "inne" (other) block
    for b in leaves:
        b.extract()
    rest = clean_text(content)
    if len(rest.replace(LINK_MARK, " ").split()) >= 5:
        blocks.append({"tag": "inne", "text": rest, "raw": rest.replace(f" {LINK_MARK} ", " ")})
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


def crawl(domain: str, sitemap: str | None = None, delay: float = 0.5) -> list[dict]:
    """Downloads the sitemap pages and writes data/<domain>/pages.jsonl."""
    base = f"https://{domain.removeprefix('https://').removeprefix('http://').strip('/')}"
    robots, declared = load_robots(base)
    delay = max(delay, float(robots.crawl_delay(ROBOTS_AGENT) or 0))
    sitemap = sitemap or find_sitemap(base, declared)
    print(f"sitemap: {sitemap}")
    urls = list(dict.fromkeys(sitemap_urls(sitemap)))
    print(f"URLs in sitemap: {len(urls)}")

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
