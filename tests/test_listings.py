import copy

from internal_linking.config import load_config
from internal_linking.crawl import extract, strip_excerpts
from internal_linking.keywords import resolve_target
from internal_linking.pipeline import find_candidates, prepare

from conftest import page

A_META = "Google zaktualizowało wytyczne dotyczące treści AI i ręczny fact-check obejmuje tytuły, opisy oraz znaczniki."
B_FIRST = ("Drugi wpis opisuje, jak przygotować znaczniki schema dla sklepu internetowego krok po kroku, "
           "od produktu po opinie klientów.")
C_FIRST = "Trzeci wpis porównuje narzędzia do monitorowania widoczności marki w odpowiedziach modeli językowych."


def post(slug: str, title: str, first: str, body: str, links: list[str]) -> dict:
    filler = " ".join([f"Wpis {slug} rozwija temat w kolejnych akapitach z przykładami i wnioskami."] * 18)
    blocks = [{"tag": "p", "text": first, "raw": first}, {"tag": "p", "text": body, "raw": body},
              {"tag": "p", "text": filler, "raw": filler}]
    return page(f"https://example.pl/blog/{slug}/", f"{title} - Blog", " ".join(b["raw"] for b in blocks),
                blocks=blocks, content_links=links)


def site() -> list[dict]:
    a = post("wytyczne-ai", "Wytyczne Google dla treści AI", "Pierwszy akapit wpisu o wytycznych dla treści AI i jego skutkach.",
             "Sprawdzaj dane strukturalne i zamów audyt SEO przed publikacją.", ["example.pl/blog/tag/ai/"])
    a["meta"] = A_META
    b = post("schema-sklep", "Schema w sklepie", B_FIRST, "Opisujemy też typy produktów.", [])
    c = post("monitoring-marki", "Monitoring marki w AI", C_FIRST, "Porównanie obejmuje ceny.", ["example.pl/blog/tag/ai/"])
    excerpts = [A_META, " ".join(B_FIRST.split()[:12]) + " …", C_FIRST]
    tag = page("https://example.pl/blog/tag/ai/", "AI - Blog", " ".join(excerpts),
               blocks=[{"tag": "p", "text": e, "raw": e} for e in excerpts],
               content_links=["example.pl/blog/wytyczne-ai/", "example.pl/blog/schema-sklep/",
                              "example.pl/blog/monitoring-marki/"])
    tag_ds = page("https://example.pl/blog/tag/dane-strukturalne/", "Dane strukturalne - Blog", "Lista wpisów.")
    category = page("https://example.pl/kategoria/audyt-seo/", "Audyt SEO - Blog", "Lista usług audytu.")
    return [a, b, c, tag, tag_ds, category]


def test_excerpts_on_a_tag_page_are_removed_and_posts_keep_their_text():
    pages = site()
    assert strip_excerpts(pages) == 3  # description, cut-short start, first paragraph among other excerpts
    a, b, c, tag = pages[:4]
    assert tag["blocks"] == [] and tag["listing"]
    # the post links to its tag page, whose excerpt repeats its first paragraph - the post keeps it
    assert c["blocks"][0]["raw"] == C_FIRST and not c.get("listing")
    assert len(a["blocks"]) == 3 and len(b["blocks"]) == 3
    assert strip_excerpts(pages) == 0 and tag["listing"]  # idempotent


def test_a_single_quote_of_a_linked_page_stays():
    pages = site()
    quote = C_FIRST  # a post quoting another post it links to, not cut short, no other excerpts
    pages[1]["blocks"].append({"tag": "blockquote", "text": quote, "raw": quote})
    pages[1]["content_links"] = ["example.pl/blog/monitoring-marki/"]
    strip_excerpts(pages)
    assert any(b["raw"] == quote for b in pages[1]["blocks"]) and not pages[1].get("listing")


def test_listings_are_never_sources_and_archives_never_targets():
    rows, info = find_candidates(site(), load_config())
    assert not {r["source_url"] for r in rows} & {"https://example.pl/blog/tag/ai/"}
    targets = {r["target_url"] for r in rows}
    assert "https://example.pl/blog/tag/dane-strukturalne/" not in targets
    assert "https://example.pl/kategoria/audyt-seo/" in targets  # categories stay targets
    assert info["listing pages (not sources)"] == 1 and info["archives (not targets)"] == 2
    # without the archive patterns the tag page would be a target
    rows, _ = find_candidates(site(), {**load_config(), "archive_patterns": []})
    assert "https://example.pl/blog/tag/dane-strukturalne/" in {r["target_url"] for r in rows}


def test_keyword_mode_never_auto_picks_an_archive():
    s = prepare(copy.deepcopy(site()), load_config())
    assert s["pages"][resolve_target("dane strukturalne", s["pages"], s["titles"])]["url"].endswith("/tag/dane-strukturalne/")
    assert resolve_target("dane strukturalne", s["pages"], s["titles"], skip=s["archives"]) is None


LISTING_HTML = """<html lang="pl"><head><title>AI - Blog</title></head><body><main><h1>AI</h1>
<a href="/blog/wpis-a/"><article><h2>Wpis A</h2><p>Zajawka pierwszego wpisu w karcie, która cała jest linkiem do wpisu.</p></article></a>
<article><h2><a href="/blog/wpis-b/">Wpis B</a></h2><p>Zajawka drugiego wpisu, której tytuł w nagłówku jest linkiem do wpisu.</p></article>
</main></body></html>"""


def test_a_listing_of_cards_is_read_whole_and_a_card_inside_a_link_takes_no_new_link():
    p = extract("https://example.pl/blog/tag/ai/", LISTING_HTML, "example.pl")
    assert p["content_links"] == ["example.pl/blog/wpis-a/", "example.pl/blog/wpis-b/"]  # not just one card
    texts = [b["text"] for b in p["blocks"]]
    assert not any("pierwszego wpisu" in t for t in texts)  # inside <a>: already link text
    assert any("drugiego wpisu" in t for t in texts)


def test_report_tells_true_orphans_from_pages_linked_only_from_the_menu():
    from internal_linking.report import page_summary
    pages = site()
    pages[1]["other_links"] = ["example.pl/blog/wytyczne-ai/"]          # post A is in a menu only
    summary = page_summary(pages, load_config())
    a, tag_ds, tag = (summary[f"https://example.pl/blog/{s}/"] for s in ("wytyczne-ai", "tag/dane-strukturalne", "tag/ai"))
    assert a["in"] == 1 and a["any"] == 2       # from the tag page's content, and from a menu
    assert tag_ds["in"] == 0 and tag_ds["any"] == 0 and tag_ds["archive"]  # a true orphan, an archive
    assert tag["listing"] and tag["archive"]
    assert "archive" not in summary["https://example.pl/kategoria/audyt-seo/"]
