import types

from internal_linking import crawl as crawl_mod
from internal_linking.common import read_jsonl, set_data_root

HTML = """<!doctype html><html lang="pl-PL"><head><title>Wpis - Serwis</title>
<meta name="description" content="Opis wpisu"><link rel="canonical" href="https://example.pl/wpis/"></head>
<body><header><nav><a href="/">Start</a><a href="/uslugi/">Usługi</a></nav></header>
<nav aria-label="Breadcrumb"><a href="/">Start</a><a href="/blog/">Blog</a></nav>
<main></main>
<article>
  <h1>Tytuł wpisu</h1>
  <h2>Nagłówek sekcji</h2>
  <p>Pierwszy akapit z <a href="/kontakt/">linkiem do kontaktu</a> w środku zdania.</p>
  <ul><li>Punkt listy o wymianie opon</li></ul>
  <pre><code>kod programu</code></pre>
</article>
<footer><a href="/regulamin/">Regulamin</a></footer></body></html>"""


def test_extract_blocks_links_and_language():
    p = crawl_mod.extract("https://example.pl/wpis/", HTML, "example.pl")
    assert p["lang"] == "pl"
    assert p["title"] == "Wpis - Serwis" and p["h1"] == "Tytuł wpisu" and p["meta"] == "Opis wpisu"
    # empty <main> next to the article: content comes from <article>
    assert "Pierwszy akapit" in p["text"] and "Regulamin" not in p["text"]
    tags = [b["tag"] for b in p["blocks"]]
    assert tags[:4] == ["h1", "h2", "p", "li"]
    para = p["blocks"][2]
    assert "¶" in para["text"] and "linkiem do kontaktu" not in para["text"]  # link text is off limits
    assert "linkiem do kontaktu" in para["raw"]                              # but kept for context
    assert all("kod programu" not in b["text"] for b in p["blocks"])
    assert p["content_links"] == ["example.pl/kontakt/"]
    assert set(p["breadcrumb_links"]) == {"example.pl/", "example.pl/blog/"}
    assert "example.pl/regulamin/" in p["other_links"]


def fake_fetch(pages: dict):
    def fetch(url):
        body = pages.get(url)
        if body is None:
            return types.SimpleNamespace(ok=False, status_code=404, text="", content=b"", url=url)
        return types.SimpleNamespace(ok=True, status_code=200, text=body, content=body.encode(), url=url)
    return fetch


def test_crawl_respects_robots_txt(tmp_path, monkeypatch):
    sitemap = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               '<url><loc>https://example.pl/wpis/</loc></url>'
               '<url><loc>https://example.pl/prywatne/strona/</loc></url></urlset>')
    monkeypatch.setattr(crawl_mod, "fetch", fake_fetch({
        "https://example.pl/robots.txt": "User-agent: *\nDisallow: /prywatne/\nSitemap: https://example.pl/sitemap.xml",
        "https://example.pl/sitemap.xml": sitemap,
        "https://example.pl/wpis/": HTML,
        "https://example.pl/prywatne/strona/": HTML,
    }))
    monkeypatch.setattr(crawl_mod.time, "sleep", lambda s: None)
    set_data_root(tmp_path)
    pages = crawl_mod.crawl("example.pl")
    assert [p["url"] for p in pages] == ["https://example.pl/wpis/"]
    assert read_jsonl(tmp_path / "example.pl" / "pages.jsonl")[0]["url"] == "https://example.pl/wpis/"


def test_template_blocks_repeated_across_pages_are_removed():
    bar = {"tag": "p", "text": "Call us now ¶", "raw": "Call us now +44 20 000", "links": ["example.com/contact/"]}
    pages = []
    for i in range(5):
        own = {"tag": "p", "text": f"Unique article text number {i}.", "raw": f"Unique article text number {i}.",
               "links": ["example.com/guide/"] if i == 0 else []}
        pages.append({"url": f"https://example.com/{i}/", "lang": "en", "blocks": [bar, own],
                      "text": f"Call us now +44 20 000 Unique article text number {i}.", "text_free": "",
                      "content_links": ["example.com/contact/"] + own["links"], "other_links": []})
    assert crawl_mod.strip_boilerplate(pages) == 5
    first = pages[0]
    assert [b["raw"] for b in first["blocks"]] == ["Unique article text number 0."]
    assert first["text"] == "Unique article text number 0."
    assert first["content_links"] == ["example.com/guide/"]        # template link no longer counts as content
    assert "example.com/contact/" in first["other_links"]


def test_small_language_groups_are_left_alone():
    block = {"tag": "p", "text": "Same text", "raw": "Same text", "links": []}
    pages = [{"url": f"https://example.com/{i}/", "lang": "pl", "blocks": [block], "text": "Same text",
              "text_free": "", "content_links": [], "other_links": []} for i in range(3)]
    assert crawl_mod.strip_boilerplate(pages) == 0


def test_block_repeated_on_five_pages_is_template_even_in_a_big_site():
    bio = {"tag": "p", "text": "Author bio", "raw": "Author bio", "links": []}
    pages = [{"url": f"https://example.com/{i}/", "lang": "en", "text": "", "text_free": "", "content_links": [],
              "other_links": [], "blocks": ([bio] if i < 5 else []) + [
                  {"tag": "p", "text": f"Text {i}", "raw": f"Text {i}", "links": []}]} for i in range(40)]
    assert crawl_mod.strip_boilerplate(pages) == 5   # 5 of 40 pages is only 12%, still a template
