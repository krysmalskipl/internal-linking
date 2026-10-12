"""Broken internal links: found in the content, checked once, repaired instead of doubled (no network)."""
import types

from internal_linking import agent, broken, pipeline
from internal_linking.common import read_jsonl
from internal_linking.config import load_config

from conftest import page

CFG = {**load_config(), "min_words": 0}
OLD = "example.pl/blog/robots.txt-w-2026-jak-skonfigurowac-(gptbot,-claudebot"


def site_with_a_broken_link() -> dict:
    text = "Zweryfikuj reguły pliku robots. Zobacz jak skonfigurować robots.txt pod crawlery AI w praktyce."
    src = page("https://example.pl/blog/aeo/", "AEO kontra SEO", text, blocks=[
        {"tag": "p", "text": text, "raw": text, "links": [OLD],
         "anchors": [[OLD, "jak skonfigurować robots.txt pod crawlery AI"], ["example.pl/plik.pdf", "PDF"]]}])
    robots = page("https://example.pl/blog/robots-txt-crawlery-ai-2026/", "Robots.txt a boty AI w 2026", "Treść o robots.txt.")
    other = page("https://example.pl/blog/lazy-loading/", "Lazy loading obrazów", "Treść o lazy loading.")
    return pipeline.prepare([src, robots, other], CFG)


def answers(monkeypatch, status: int, final: str | None = None, html: str = "<title>Strona</title>"):
    calls = []

    def get(url, timeout, allow_redirects):
        calls.append(url)
        return types.SimpleNamespace(status_code=status, url=final or url, text=html)
    monkeypatch.setattr(broken.session, "get", get)
    monkeypatch.setattr(broken.time, "sleep", lambda s: None)
    return calls


def test_a_404_link_is_found_with_the_page_it_was_meant_for(tmp_path, monkeypatch):
    calls = answers(monkeypatch, 404)
    found = broken.find(site_with_a_broken_link(), tmp_path)
    assert calls == [f"https://{OLD}"]  # files (the PDF) are not checked
    assert len(found) == 1
    b = found[0]
    assert b["status"] == 404 and b["anchor"] == "jak skonfigurować robots.txt pod crawlery AI"
    assert b["suggested_url"] == "https://example.pl/blog/robots-txt-crawlery-ai-2026/"
    assert "robots.txt pod crawlery AI" in b["context"]
    # checked once a week: a second run sends nothing
    broken.find(site_with_a_broken_link(), tmp_path)
    assert len(calls) == 1 and read_jsonl(tmp_path / broken.CACHE)[0]["status"] == 404


def test_a_redirect_to_a_working_page_or_another_answer_is_not_broken(tmp_path, monkeypatch):
    answers(monkeypatch, 200, "https://example.pl/blog/robots-txt-crawlery-ai-2026/")
    assert broken.find(site_with_a_broken_link(), tmp_path) == []
    answers(monkeypatch, 503)  # a server error is not proof the page is gone
    (tmp_path / "fresh").mkdir()
    assert broken.find(site_with_a_broken_link(), tmp_path / "fresh") == []


def test_a_not_found_page_served_with_200_is_broken_too(tmp_path, monkeypatch):
    answers(monkeypatch, 200, html='<html><head><title>Wpis nie znaleziony - Example</title><meta name="robots" content="noindex">')
    found = broken.find(site_with_a_broken_link(), tmp_path)
    assert [b["status"] for b in found] == ["soft 404"]
    answers(monkeypatch, 200, html="<title>Jak skonfigurować robots.txt</title>")  # a real page outside the sitemap
    (tmp_path / "real").mkdir()
    assert broken.find(site_with_a_broken_link(), tmp_path / "real") == []


def test_the_agent_file_lists_the_repairs_and_verify_still_reads_it(tmp_path):
    b = {"source_url": "https://example.pl/blog/aeo/", "anchor": "robots.txt pod crawlery AI", "status": 404,
         "broken_url": f"https://{OLD}", "suggested_url": "https://example.pl/blog/robots-txt-crawlery-ai-2026/",
         "suggested_title": "Robots.txt a boty AI", "context": "…robots.txt pod crawlery AI…"}
    link = {"source_url": "https://example.pl/a/", "anchor": "lazy loading", "target_url": "https://example.pl/b/",
            "context": "o lazy loading"}
    md = agent.agent_markdown("example.pl", [link], broken=[b])
    assert "## Broken links to repair" in md and "→ change to `/blog/robots-txt-crawlery-ai-2026/`" in md
    (tmp_path / "links.md").write_text(md, encoding="utf-8")
    assert [r["anchor"] for r in agent.read_links(tmp_path / "links.md")] == ["lazy loading"]


def test_run_repairs_a_broken_link_instead_of_adding_a_new_one(tmp_path, monkeypatch):
    from internal_linking import cli
    from internal_linking.common import read_csv, write_jsonl
    from internal_linking.jev import pairs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(pairs, "judge", lambda state, lang: {"answers": {
        "kontekst": {"noul": 0.9}, "anchor": {"noul": 0.9}, "wartosc": {"score": 3.0}, "kanibalizacja": {"noul": 0.1},
        "powod": {"choice": "ok", "probabilities": {"ok": 0.97}}}})
    ddir = tmp_path / "data" / "example.pl"
    ddir.mkdir(parents=True)
    (tmp_path / "config.json").write_text('{"min_words": 0}', encoding="utf-8")
    pages = site_with_a_broken_link()["pages"]
    pages[0]["blocks"].append({"tag": "p", "text": "Dobrze ustawione Robots.txt a boty AI to podstawa.",
                               "raw": "Dobrze ustawione Robots.txt a boty AI to podstawa."})
    write_jsonl(ddir / "pages.jsonl", pages)
    pair = ("https://example.pl/blog/aeo/", "https://example.pl/blog/robots-txt-crawlery-ai-2026/")

    answers(monkeypatch, 200, "https://example.pl/")  # the old address works: the phrase match is proposed
    cli.main(["--data-dir", "data", "run", "--domain", "example.pl", "--no-crawl", "--no-embeddings"])
    assert pair in {(r["source_url"], r["target_url"]) for r in read_csv(ddir / "recommendations.csv")}
    assert not (ddir / "broken_links.csv").exists()

    (ddir / broken.CACHE).unlink()
    answers(monkeypatch, 404)  # now it is gone: repair it, no second link to the same page
    cli.main(["--data-dir", "data", "run", "--domain", "example.pl", "--no-crawl", "--no-embeddings"])
    assert pair not in {(r["source_url"], r["target_url"]) for r in read_csv(ddir / "recommendations.csv")}
    assert read_csv(ddir / "broken_links.csv")[0]["suggested_url"] == pair[1]
    assert "## Broken links to repair" in (ddir / "links-for-agent.md").read_text(encoding="utf-8")
    assert "Broken links" in (ddir / "report.html").read_text(encoding="utf-8")
