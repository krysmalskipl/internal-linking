from internal_linking.config import load_config
from internal_linking.jev import pairs
from internal_linking.pipeline import already_linked, clean_title, find_candidates, select, site_suffixes, sitewide_links

from conftest import page

CFG = {"score_min": 0.9, "context_min": 0.5, "anchor_min": 0.5, "cannibalisation_max": 0.7}


def row(src, anchor, target, score, context=0.9, anchor_p=0.9, cannib=0.1, verdict="ok"):
    return {"source_url": src, "anchor": anchor, "target_url": target, "score": score, "jev_context": context,
            "jev_anchor": anchor_p, "jev_cannibalisation": cannib, "jev_verdict": verdict}


def test_select_applies_rules_limit_and_one_link_per_phrase():
    rows = [
        row("/a", "opon zimowych", "/opony", 0.99),
        row("/a", "opon zimowych", "/zima", 0.95),        # same phrase, weaker target
        row("/a", "geometrii kół", "/geometria", 0.97),
        row("/a", "serwis klimatyzacji", "/klima", 0.96),
        row("/a", "audyt instalacji", "/audyt", 0.93),     # over the per-page limit of 3
        row("/a", "tanie opony", "/tanie", 0.5, verdict="too_generic"),
        row("/b", "remont łazienki", "/lazienki", 0.98, cannib=0.8),
        row("/b", "gładzie gipsowe", "/gladzie", 0.98, context=0.3),
    ]
    select(rows, CFG, max_links=3)
    by = {(r["source_url"], r["target_url"]): r["decision_reason"] for r in rows}
    assert by[("/a", "/opony")] == "" and by[("/a", "/geometria")] == "" and by[("/a", "/klima")] == ""
    assert by[("/a", "/zima")] == "duplicate_phrase"
    assert by[("/a", "/audyt")] == "limit"
    assert by[("/a", "/tanie")] == "too_generic"
    assert by[("/b", "/lazienki")] == "cannibalisation"
    assert by[("/b", "/gladzie")] == "off_topic"
    assert sum(r["decision"] == "accept" for r in rows) == 3


def test_menu_links_do_not_count_as_already_linked():
    pages = [page(f"https://example.pl/p{i}/", f"Strona {i}", "tekst", other_links=["example.pl/uslugi/"])
             for i in range(4)]
    pages[0]["other_links"].append("example.pl/powiazany/")
    pages[0]["content_links"] = ["example.pl/kontakt/"]
    sitewide = sitewide_links(pages)
    assert sitewide == {"pl": {"example.pl/uslugi/"}}
    assert already_linked(pages[0], sitewide) == {"example.pl/kontakt/", "example.pl/powiazany/"}


def test_title_suffix_is_removed():
    pages = [page(f"https://example.pl/{i}/", f"Tytuł {i} - Firma Remontowa", "t") for i in range(4)]
    assert clean_title("Tytuł 1 - Firma Remontowa", site_suffixes(pages)) == "Tytuł 1"


def test_candidates_stay_within_one_language(site):
    site[1]["lang"] = "en"   # the tyre page belongs to the English version
    home = "Robimy też serwis klimatyzacji samochodowej i wymianę opon zimowych. " + site[0]["text"]
    site[0].update(text=home, blocks=[{"tag": "p", "text": home, "raw": home}])
    rows, _ = find_candidates(site, load_config())
    targets = {r["target_url"] for r in rows}
    assert "https://example.pl/klimatyzacja/" in targets
    assert "https://example.pl/wymiana-opon/" not in targets
    assert all(r["placement"] == "paragraph" and r["anchor"] in r["context"] for r in rows)


def test_reject_reason_maps_verdict_when_score_is_low():
    r = row("/a", "x y", "/t", 0.4, verdict="wrong_intent")
    assert pairs.reject_reason(r, CFG) == "wrong_intent"
    r = row("/a", "x y", "/t", 0.4, verdict="ok")
    assert pairs.reject_reason(r, CFG) == "low_score"


def test_declared_language_version_wins_and_url_prefix_is_a_fallback(site):
    from internal_linking.anchors import detect_lang
    assert detect_lang({**site[0], "lang": "en"}) == "en"   # the declared version, even with Polish text
    assert detect_lang({**site[0], "lang": "", "url": "https://example.pl/en/page/"}) == "en"
    assert detect_lang({**site[0], "lang": ""}) == "pl"     # no declaration: the text decides


def test_template_links_count_per_language_version():
    pl = [page(f"https://example.pl/p{i}/", f"Strona {i}", "t", other_links=["example.pl/menu-pl/"]) for i in range(6)]
    en = [page(f"https://example.pl/en/p{i}/", f"Page {i}", "t", lang="en",
               other_links=["example.pl/en/sidebar/"] if i < 2 else []) for i in range(4)]
    sitewide = sitewide_links(pl + en)
    # the English sidebar link is on half of the English pages - a template link there,
    # even though it is on only 2 of 10 pages overall
    assert "example.pl/en/sidebar/" not in already_linked(en[0], sitewide)
