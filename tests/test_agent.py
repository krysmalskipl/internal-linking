import types

import pytest

from internal_linking import agent, pipeline
from internal_linking.common import set_data_root, write_csv, write_jsonl
from internal_linking.config import load_config
from internal_linking.jev import pairs


def link(source: str, anchor: str, target: str, context: str) -> dict:
    return {"source_url": f"https://example.pl{source}", "source_title": f"Page {source}", "anchor": anchor,
            "target_url": f"https://example.pl{target}", "target_title": f"Target {target}", "context": context}


ROWS = [link("/blog/a/", "dane strukturalne", "/uslugi/schema/", "Sprawdź dane strukturalne, zanim opublikujesz."),
        link("/blog/a/", "audyt SEO", "/uslugi/audyt/", 'Zamów audyt SEO - "pełny", z raportem,\n  w PDF.'),
        link("/blog/b/", "pozycjonowanie", "/uslugi/seo/?lang=pl", "Pozycjonowanie lokalne trwa.")]


def test_agent_file_groups_links_by_page_and_carries_the_instructions():
    md = agent.agent_markdown("example.pl", ROWS, "thresholds: score ≥ 90%")
    assert md.startswith("# Internal links to insert - example.pl\n\n3 links on 2 pages")
    assert "## Instructions for the coding agent" in md and "verify --domain example.pl" in md
    assert md.count("### /blog/a/ - Page /blog/a/") == 1 and "### /blog/b/" in md  # one section per file to edit
    assert "1. Link text: `dane strukturalne` → `/uslugi/schema/` - Target /uslugi/schema/" in md
    assert "`/uslugi/seo/?lang=pl`" in md  # root-relative, query kept
    assert '   > Zamów audyt SEO - "pełny", z raportem, w PDF.' in md  # the passage on one line


def test_verify_reads_back_exactly_the_links_given_to_the_agent(tmp_path):
    path = tmp_path / "links-for-agent.md"
    path.write_text(agent.agent_markdown("example.pl", ROWS), encoding="utf-8")
    back = agent.read_links(path)
    assert [(r["source_url"], r["anchor"], r["target_url"]) for r in back] == \
        [(r["source_url"], r["anchor"], r["target_url"]) for r in ROWS]
    assert back[1]["context"] == 'Zamów audyt SEO - "pełny", z raportem, w PDF.'
    csv_path = tmp_path / "links.csv"
    write_csv(csv_path, ROWS, ["source_url", "source_title", "anchor", "target_url"])
    assert len(agent.read_links(csv_path)) == 3
    (tmp_path / "notes.md").write_text("# just notes")
    with pytest.raises(RuntimeError, match="no ```csv block"):
        agent.read_links(tmp_path / "notes.md")


def test_run_writes_the_agent_file(site, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    set_data_root(tmp_path / "data")
    home = "Robimy też serwis klimatyzacji samochodowej i wymianę opon zimowych. " + site[0]["text"]
    site[0].update(text=home, blocks=[{"tag": "p", "text": home, "raw": home}])
    ddir = tmp_path / "data" / "example.pl"
    ddir.mkdir(parents=True)
    write_jsonl(ddir / "pages.jsonl", site)
    monkeypatch.setattr(pairs, "judge", lambda state, lang="pl": {"answers": {
        "kontekst": {"noul": 0.9}, "anchor": {"noul": 0.9}, "wartosc": {"score": 3.0}, "kanibalizacja": {"noul": 0.1},
        "powod": {"choice": "ok", "probabilities": {"ok": 0.97}}}})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    args = types.SimpleNamespace(no_crawl=True, sitemap=None, delay=0, sources_file=None, targets_file=None, keywords=None,
                                 sources=None, targets=None, dry_run=False, no_embeddings=True, workers=2, max_links=None)
    result = pipeline.process("example.pl", args, load_config())
    links = agent.read_links(ddir / "links-for-agent.md")
    assert len(links) == result["links"] > 0
    assert "at most 3 per page" in (ddir / "links-for-agent.md").read_text(encoding="utf-8")
