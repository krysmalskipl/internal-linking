from internal_linking.jev import pairs

from conftest import page


def response(p_ok=0.95, verdict="ok"):
    return {"answers": {
        "kontekst": {"type": "noul", "noul": 0.9},
        "anchor": {"type": "noul", "noul": 0.8},
        "wartosc": {"type": "score", "score": 3.0},
        "kanibalizacja": {"type": "noul", "noul": 0.1},
        "powod": {"type": "choice", "choice": verdict, "probabilities": {"ok": p_ok}},
    }}


def test_summarize_maps_answers_to_english_columns():
    out = pairs.summarize(response(verdict="temat_niezwiazany", p_ok=0.3))
    assert out == {"score": 0.3, "jev_context": 0.9, "jev_anchor": 0.8, "jev_value": 0.75,
                   "jev_cannibalisation": 0.1, "jev_verdict": "off_topic"}


def test_questions_cover_every_answer_used_by_summarize():
    assert set(pairs.QUESTIONS) == {"kontekst", "anchor", "wartosc", "kanibalizacja", "powod"}
    assert set(pairs.QUESTIONS["powod"]["criteria"]) == set(pairs.VERDICTS)


def test_context_is_trimmed_around_the_phrase():
    raw = "a" * 1000 + " fraza linku " + "b" * 1000
    ctx = pairs.context({"tag": "p", "text": raw, "raw": raw}, "fraza linku")
    assert "fraza linku" in ctx and len(ctx) < 700 and ctx.startswith("…") and ctx.endswith("…")


def test_cache_key_is_stable_and_depends_on_state():
    src, tgt = page("https://example.pl/a/", "A", "tekst"), page("https://example.pl/b/", "B", "tekst")
    blocks = [{"tag": "p", "text": "tekst", "raw": "tekst"}]
    s1 = pairs.build_state(src, "A", blocks, 0, tgt, "B", "tekst")
    s2 = pairs.build_state(src, "A", blocks, 0, tgt, "B", "tekst")
    assert pairs.cache_key(s1) == pairs.cache_key(s2)
    assert pairs.cache_key(s1) != pairs.cache_key({**s1, "fraza_linku": "inna"})


def test_english_pairs_use_english_questions_and_keys():
    src, tgt = page("https://example.com/a/", "A", "text", lang="en"), page("https://example.com/b/", "B", "text", lang="en")
    blocks = [{"tag": "li", "text": "text", "raw": "text"}]
    state = pairs.build_state(src, "A", blocks, 0, tgt, "B", "text", "en")
    assert state["placement"] == "list item" and state["link_phrase"] == "text"
    assert pairs.cache_key(state, "en") != pairs.cache_key(state, "pl")
    resp = {"answers": {
        "context": {"noul": 0.7}, "anchor": {"noul": 0.6}, "value": {"score": 2.0},
        "cannibalisation": {"noul": 0.2},
        "verdict": {"choice": "wrong_intent", "probabilities": {"ok": 0.2}},
    }}
    assert pairs.summarize(resp, "en") == {"score": 0.2, "jev_context": 0.7, "jev_anchor": 0.6, "jev_value": 0.5,
                                           "jev_cannibalisation": 0.2, "jev_verdict": "wrong_intent"}
    assert set(pairs.QUESTIONS_EN["verdict"]["criteria"]) == set(pairs.VERDICTS.values())


def test_state_carries_section_heading_and_neighbouring_passages():
    blocks = [{"tag": "h2", "text": "Opony", "raw": "Opony"},
              {"tag": "p", "text": "Akapit przed.", "raw": "Akapit przed."},
              {"tag": "h3", "text": "Zima", "raw": "Zima"},
              {"tag": "p", "text": "Wymiana opon zimowych.", "raw": "Wymiana opon zimowych."},
              {"tag": "li", "text": "Punkt po.", "raw": "Punkt po."}]
    src, tgt = page("https://example.pl/a/", "A", "t"), page("https://example.pl/b/", "B", "t")
    state = pairs.build_state(src, "A", blocks, 3, tgt, "B", "opon zimowych")
    assert state["naglowek_sekcji"] == "Zima"
    assert state["poprzedni_fragment"] == "Akapit przed." and state["nastepny_fragment"] == "Punkt po."
    assert state["fragment_z_fraza"] == "Wymiana opon zimowych."
