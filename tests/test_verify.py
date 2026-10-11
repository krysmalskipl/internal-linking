import types
import urllib.robotparser

import pytest

from internal_linking import agent, pipeline, verify
from internal_linking.common import read_csv, set_data_root, write_jsonl
from internal_linking.config import load_config
from internal_linking.crawl import extract
from internal_linking.jev import pairs

FILLER = "<p>" + " ".join(["Akapit wypełniający treść wpisu o sklepie i jego widoczności w wyszukiwarce."] * 3) + "</p>"
PAGE = """<html lang="pl"><head><title>Wpis A</title></head><body>
<nav><a href="/kontakt/">Kontakt</a></nav>
<main><h1>Wpis A</h1>
<p>Sprawdź <a href="/uslugi/schema/">dane strukturalne</a>, zanim opublikujesz wpis.</p>
<p>Zamów audyt SEO, a szczegóły znajdziesz <a href="https://example.pl/uslugi/audyt/">tutaj</a>.</p>
<p>Inny akapit, w którym <a href="/uslugi/seo/">pozycjonowanie</a> pojawia się przy okazji.</p>
<p>Lokalne <a href="https://www.example.pl/uslugi/lokalne">pozycjonowanie lokalne</a> trwa kilka miesięcy.</p>
<p>Raz <a href="/uslugi/dwa/">dwa razy</a> i jeszcze raz <a href="/uslugi/dwa/">dwa razy</a> w akapicie.</p>
""" + FILLER + "</main><footer><a href=\"/regulamin/\">Regulamin</a></footer></body></html>"


def link(target: str, anchor: str, context: str, source: str = "/blog/a/") -> dict:
    return {"source_url": f"https://example.pl{source}", "anchor": anchor, "target_url": f"https://example.pl{target}",
            "context": context}


LINKS = [
    link("/uslugi/schema/", "dane strukturalne", "Sprawdź dane strukturalne, zanim opublikujesz wpis."),
    link("/uslugi/audyt/", "audyt SEO", "Zamów audyt SEO, a szczegóły znajdziesz tutaj."),
    link("/uslugi/seo/", "pozycjonowanie", "Pozycjonowanie to proces na miesiące, nie na dni."),
    link("/kontakt/", "skontaktuj się", "Napisz albo skontaktuj się z nami."),
    link("/uslugi/brak/", "linkowanie wewnętrzne", "Linkowanie wewnętrzne pomaga robotom."),
    link("/uslugi/lokalne/", "Pozycjonowanie lokalne", "Lokalne pozycjonowanie lokalne trwa kilka miesięcy."),
    link("/uslugi/dwa/", "dwa razy", "Raz dwa razy i jeszcze raz dwa razy w akapicie."),
]
EXPECTED = ["ok", "other_text", "other_place", "outside_content", "missing", "ok", "ok"]


def test_every_status_on_a_real_page():
    page = extract("https://example.pl/blog/a/", PAGE, "example.pl")
    got = [verify.check(page, lk) for lk in LINKS]
    assert [g[0] for g in got] == EXPECTED
    assert got[1][1] == "tutaj"                       # the link text that was used instead
    assert got[5][1] == "pozycjonowanie lokalne"      # www, no trailing slash, other case: the same link
    assert got[6][2] == "linked 2 times on the page"
    assert verify.check(None, LINKS[0])[0] == "page_error"


def fake_fetch(pages: dict):
    def fetch(url):
        if url not in pages:
            return None if url.endswith("robots.txt") else types.SimpleNamespace(status_code=404, text="", url=url)
        status, html, final = pages[url] if isinstance(pages[url], tuple) else (200, pages[url], url)
        return types.SimpleNamespace(status_code=status, text=html, url=final)
    return fetch


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    set_data_root(tmp_path / "data")
    ddir = tmp_path / "data" / "example.pl"
    ddir.mkdir(parents=True)
    (ddir / "links-for-agent.md").write_text(agent.agent_markdown("example.pl", LINKS + [
        link("/uslugi/schema/", "schema", "Schema to język znaczników.", source="/blog/b/")]), encoding="utf-8")
    monkeypatch.setattr(verify.time, "sleep", lambda s: None)
    allow_all = urllib.robotparser.RobotFileParser()
    allow_all.parse([])
    monkeypatch.setattr(verify, "load_robots", lambda base: (allow_all, []))  # no network in tests
    return ddir


def test_verify_on_a_local_preview_reads_its_own_absolute_links(data, monkeypatch):
    base = "http://localhost:4321"
    preview = PAGE.replace('href="https://example.pl/uslugi/audyt/"', f'href="{base}/uslugi/audyt/"')
    pages = {f"{base}/blog/a/": preview, f"{base}/uslugi/schema/": "<html></html>", f"{base}/uslugi/audyt/": "<html></html>",
             f"{base}/uslugi/seo/": "<html></html>", f"{base}/uslugi/dwa/": "<html></html>",
             # astro dev drops the trailing slash: not a redirect worth reporting
             f"{base}/uslugi/lokalne/": (200, "<html></html>", f"{base}/uslugi/lokalne")}
    monkeypatch.setattr(verify, "fetch", fake_fetch(pages))
    results = verify.verify("example.pl", base_url=base)
    assert [r["status"] for r in results] == EXPECTED + ["page_error"]   # /blog/b/ is not on the preview
    assert results[1]["found_text"] == "tutaj" and results[0]["where"] == base
    assert [r["status"] for r in read_csv(data / "verify.csv")] == EXPECTED + ["page_error"]


def test_a_target_that_does_not_load_is_reported(data, monkeypatch):
    live = "https://example.pl"
    pages = {f"{live}/blog/a/": PAGE, f"{live}/blog/b/": "<html><body><main><p>bez linku</p></main></body></html>",
             f"{live}/uslugi/audyt/": "ok", f"{live}/uslugi/seo/": "ok", f"{live}/uslugi/dwa/": "ok",
             f"{live}/uslugi/lokalne/": (200, "ok", f"{live}/nowe-lokalne/")}  # schema: 404
    monkeypatch.setattr(verify, "fetch", fake_fetch(pages))
    results = {(r["source_url"], r["target_url"]): r for r in verify.verify("example.pl")}
    schema = results[(f"{live}/blog/a/", f"{live}/uslugi/schema/")]
    assert schema["status"] == "target_error" and schema["note"] == "target: status 404"
    assert results[(f"{live}/blog/a/", f"{live}/uslugi/lokalne/")]["note"] == "target redirects to /nowe-lokalne/"
    assert results[(f"{live}/blog/b/", f"{live}/uslugi/schema/")]["status"] == "missing"


def test_verify_command_exits_with_1_when_something_is_not_ok(data, monkeypatch):
    monkeypatch.setattr(verify, "fetch", fake_fetch({}))
    with pytest.raises(SystemExit) as e:
        verify.verify_command(types.SimpleNamespace(domain="example.pl", links=None, base_url=None, delay=0, config=None))
    assert e.value.code == 1


def test_run_checks_the_previous_list_on_the_fresh_crawl(data, site, monkeypatch):
    page = extract("https://example.pl/blog/a/", PAGE, "example.pl")
    write_jsonl(data / "pages.jsonl", site + [page])
    monkeypatch.setattr(pipeline, "crawl", lambda *a, **k: None)  # the crawl "happened": pages.jsonl is fresh
    monkeypatch.setattr(pairs, "judge", lambda state, lang="pl": {"answers": {
        "kontekst": {"noul": 0.9}, "anchor": {"noul": 0.9}, "wartosc": {"score": 3.0}, "kanibalizacja": {"noul": 0.1},
        "powod": {"choice": "ok", "probabilities": {"ok": 0.97}}}})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    args = types.SimpleNamespace(no_crawl=False, sitemap=None, delay=0, sources_file=None, targets_file=None, keywords=None,
                                 sources=None, targets=None, dry_run=False, no_embeddings=True, workers=2, max_links=None)
    pipeline.process("example.pl", args, load_config())
    results = read_csv(data / "verify.csv")
    assert [r["status"] for r in results][:7] == EXPECTED and results[0]["where"] == "crawl"
    assert "target not in this crawl" in results[0]["note"]
    assert (data / "links-previous.md").exists()
    assert "Inserted (verified)" in (data / "report.html").read_text(encoding="utf-8")
