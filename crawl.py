"""Krok 1: sitemap.xml → data/<domena>/pages.jsonl (zwykle wołany przez run.py)

Dla każdej strony z sitemapy zapisuje tytuł, h1, meta description, treść główną, bloki treści
z typem (akapit, punkt listy, nagłówek...) oraz linki wewnętrzne: w treści, w okruszkach
i wszystkie pozostałe (menu, stopka, sidebar).

  python crawl.py --domain example.com
"""
import argparse
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from common import data_dir, is_internal, norm_url, write_jsonl

UA = "Mozilla/5.0 (compatible; internal-linking-audit/1.0)"
NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
LINK_MARK = "¶"  # granica w text_free: istniejący link, kod albo koniec bloku
BLOCKS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "dt", "dd", "blockquote", "figcaption"]
BOILERPLATE = ["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "svg"]

session = requests.Session()
session.headers["User-Agent"] = UA


def fetch(url: str) -> requests.Response | None:
    try:
        return session.get(url, timeout=30)
    except requests.RequestException as e:
        print(f"  ! {url}: {e}")
        return None


def parse_sitemap(xml: bytes) -> ET.Element | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    return root if root.tag in (f"{NS}urlset", f"{NS}sitemapindex") else None


def find_sitemap(base: str) -> str:
    candidates = []
    r = fetch(f"{base}/robots.txt")
    if r is not None and r.ok:
        candidates += [ln.split(":", 1)[1].strip() for ln in r.text.splitlines()
                       if ln.lower().startswith("sitemap:")]
    candidates += [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml", f"{base}/wp-sitemap.xml"]
    for url in candidates:
        r = fetch(url)
        if r is not None and r.ok and parse_sitemap(r.content) is not None:
            return url
    raise SystemExit(f"Nie znaleziono sitemapy dla {base} (sprawdzone: {', '.join(candidates)})")


def sitemap_urls(url: str, seen: set[str] | None = None) -> list[str]:
    seen = seen if seen is not None else set()
    if url in seen:
        return []
    seen.add(url)
    r = fetch(url)
    root = parse_sitemap(r.content) if r is not None and r.ok else None
    if root is None:
        print(f"  ! pominięto sitemapę {url}")
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
    """<main> (lub <body>), chyba że jeden <article> zawiera większość jego tekstu - wtedy ten
    <article>. Chroni przed wybraniem kafelka z listy wpisów jako treści strony."""
    body = soup.body or soup
    main = soup.find("main")
    # niektóre motywy mają pusty <main>, a artykuł obok niego
    body_len = len(body.get_text(" ", strip=True))
    container = main if main and len(main.get_text(" ", strip=True)) >= 0.3 * body_len else body
    total = len(container.get_text(" ", strip=True)) or 1
    articles = container.find_all("article")
    if articles:
        best = max(articles, key=lambda a: len(a.get_text(" ", strip=True)))
        if len(best.get_text(" ", strip=True)) >= 0.5 * total:
            return best
    return container


BREADCRUMB_HINTS = ("breadcrumb", "okruszk")


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

    # linki spoza treści zbieramy przed wycięciem nawigacji z kontenera treści
    all_links = internal_links(soup, url, domain)
    breadcrumb_links = set()
    for bc in soup.find_all(is_breadcrumb):
        breadcrumb_links |= internal_links(bc, url, domain)

    content = main_content(soup)
    for tag in content.find_all(BOILERPLATE):
        tag.decompose()
    content_links = internal_links(content, url, domain)
    text = clean_text(content)
    # bloki-liście (akapit, punkt listy, nagłówek...) bez zagnieżdżonych bloków
    leaves = [b for b in content.find_all(BLOCKS) if not b.find(BLOCKS)]
    raw = [clean_text(b) for b in leaves]
    # tekst istniejących linków i kodu zastępujemy separatorem, żeby nie proponować go jako nowego
    # anchora; separator na końcu bloków nie pozwala frazie przejść z nagłówka do akapitu
    for a in content.find_all(["a", "pre", "code"]):
        a.replace_with(f" {LINK_MARK} ")
    blocks = [{"tag": b.name, "text": clean_text(b), "raw": r} for b, r in zip(leaves, raw)]
    for block in content.find_all(BLOCKS):
        block.append(f" {LINK_MARK} ")
    text_free = clean_text(content)
    # tekst poza blokami (np. goły tekst w <div>) jako jeden blok "inne"
    for b in leaves:
        b.extract()
    rest = clean_text(content)
    if len(rest.replace(LINK_MARK, " ").split()) >= 5:
        blocks.append({"tag": "inne", "text": rest, "raw": rest.replace(f" {LINK_MARK} ", " ")})
    blocks = [b for b in blocks if b["text"].replace(LINK_MARK, "").strip()]

    html_tag = soup.find("html")
    lang = (html_tag.get("lang") or "").split("-")[0].lower() if html_tag else ""
    return {
        "url": url,
        "lang": lang,
        "title": title,
        "h1": h1.get_text(" ", strip=True) if h1 else "",
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
    """Pobiera strony z sitemapy i zapisuje data/<domena>/pages.jsonl."""
    base = f"https://{domain.removeprefix('https://').removeprefix('http://').strip('/')}"
    sitemap = sitemap or find_sitemap(base)
    print(f"sitemapa: {sitemap}")
    urls = list(dict.fromkeys(sitemap_urls(sitemap)))
    print(f"URL-i w sitemapie: {len(urls)}")

    pages, skipped = [], {}
    for i, url in enumerate(urls, 1):
        r = fetch(url)
        time.sleep(delay)
        if r is None or r.status_code != 200:
            skipped[url] = f"status {getattr(r, 'status_code', 'błąd')}"
            continue
        if norm_url(r.url) != norm_url(url):
            skipped[url] = f"przekierowanie na {r.url}"
            continue
        page = extract(url, r.text, domain)
        if "noindex" in page["robots"]:
            skipped[url] = "noindex"
        elif norm_url(page["canonical"]) != norm_url(url):
            skipped[url] = f"canonical → {page['canonical']}"
        else:
            pages.append(page)
            print(f"[{i}/{len(urls)}] {url}  ({len(page['text'].split())} słów, "
                  f"linki: {len(page['content_links'])} w treści, {len(page['breadcrumb_links'])} w okruszkach, "
                  f"{len(page['other_links'])} poza treścią)")

    out = data_dir(domain) / "pages.jsonl"
    write_jsonl(out, pages)
    print(f"\nZapisano {len(pages)} stron → {out}")
    if skipped:
        print(f"Pominięto {len(skipped)}:")
        for url, why in skipped.items():
            print(f"  - {url}: {why}")
    return pages


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", required=True, help="np. example.com")
    ap.add_argument("--sitemap", help="adres sitemapy, jeśli nie ma jej w robots.txt")
    ap.add_argument("--delay", type=float, default=0.5, help="przerwa między zapytaniami (s)")
    args = ap.parse_args()
    crawl(args.domain, args.sitemap, args.delay)


if __name__ == "__main__":
    main()
