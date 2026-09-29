from internal_linking.config import load_config
from internal_linking.keywords import load_keywords, resolve
from internal_linking.pipeline import keyword_candidates, prepare

from conftest import page


def test_load_csv_and_plain_text(tmp_path):
    csv_file = tmp_path / "k.csv"
    csv_file.write_text("keyword,target_url,match\nwymiana opon zimowych,https://example.pl/wymiana-opon/,exact\n"
                        "geometria kół,,\n")
    assert load_keywords(csv_file) == [
        {"keyword": "wymiana opon zimowych", "target_url": "https://example.pl/wymiana-opon/", "match": "exact"},
        {"keyword": "geometria kół", "target_url": "", "match": "partial"}]
    txt = tmp_path / "k.txt"
    txt.write_text("serwis klimatyzacji\n# komentarz\n\n")
    assert load_keywords(txt) == [{"keyword": "serwis klimatyzacji", "target_url": "", "match": "partial"}]


def test_target_is_picked_by_title_when_missing(site):
    s = prepare(site, load_config())
    resolved, warnings = resolve([{"keyword": "ustawienie geometrii", "target_url": "", "match": "partial"},
                                  {"keyword": "loty w kosmos", "target_url": "", "match": "partial"}],
                                 s["pages"], s["titles"])
    assert [r["target_url"] for r in resolved] == ["https://example.pl/geometria/"]
    assert resolved[0]["auto_target"] and len(warnings) == 1


def test_keyword_mode_links_only_listed_phrases(site):
    keywords = [{"keyword": "wymiana opon zimowych", "target_url": "https://example.pl/wymiana-opon/",
                 "match": "exact"}]
    rows, info, resolved = keyword_candidates(site, load_config(), keywords)
    assert {r["source_url"] for r in rows} == {"https://example.pl/geometria/"}  # never the target itself
    assert all(r["keyword"] == "wymiana opon zimowych" and r["anchor_type"] == "exact" for r in rows)


def test_one_word_keyword_is_allowed_in_keyword_mode():
    filler = " ".join(["Serwis realizuje zlecenia na terenie miasta."] * 25)
    pages = [page("https://example.pl/", "Start", "Naprawiamy też klimatyzację w autach. " + filler),
             page("https://example.pl/klima/", "Klimatyzacja", filler)]
    rows, _, _ = keyword_candidates(pages, load_config(),
                                    [{"keyword": "klimatyzacja", "target_url": "", "match": "exact"}])
    assert [(r["source_url"], r["anchor"]) for r in rows] == [("https://example.pl/", "klimatyzację")]
