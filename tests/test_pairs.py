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
    block = {"tag": "p", "text": "tekst", "raw": "tekst"}
    s1 = pairs.build_state(src, "A", block, tgt, "B", "tekst")
    s2 = pairs.build_state(src, "A", block, tgt, "B", "tekst")
    assert pairs.cache_key(s1) == pairs.cache_key(s2)
    assert pairs.cache_key(s1) != pairs.cache_key({**s1, "fraza_linku": "inna"})
