"""Query fan-out and semantic phrases: GSC import, sub-queries, who answers them, and the link
candidates they give - with stand-ins for the model, the embeddings and Jev (no network)."""
import argparse
import json
import zlib

import numpy as np
import pytest

from internal_linking import cli, embeddings, fanout, passages, pipeline
from internal_linking.anchors import content_words, word_key
from internal_linking.common import read_csv, read_jsonl, write_jsonl
from internal_linking.config import load_config
from internal_linking.crawl import LINK_MARK
from internal_linking.jev import client, pairs

from conftest import page

ANSWER = {"answers": {"kontekst": {"noul": 0.9}, "anchor": {"noul": 0.9}, "wartosc": {"score": 3.0},
                      "kanibalizacja": {"noul": 0.1}, "powod": {"choice": "ok", "probabilities": {"ok": 0.97}}}}
SUBQUERIES = [
    {"query": "kiedy zmienić opony na zimowe", "type": "how-to"},     # the hub answers it
    {"query": "ciśnienie powietrza kompresor", "type": "related"},   # a page the hub does not link to
    {"query": "geometria zawieszenia prowadzenie", "type": "related"},  # a page the hub links to
    {"query": "cena wulkanizacji mobilnej", "type": "cost"},         # nobody
    {"query": "przechowywanie kół sezonowe", "type": "related"},      # nobody
]
CFG = {**load_config(), "min_words": 0, "fanout_answer_min": 0.4, "fanout_merge_min": 0.88, "semantic_min": 0.4}


def para(text: str) -> dict:
    return {"tag": "p", "text": text, "raw": text}


def tyre_site() -> list[dict]:
    hub = page("https://example.pl/opony/", "Opony zimowe", "x", blocks=[
        para("Kiedy zmienić opony na zimowe? Gdy temperatura spada poniżej siedmiu stopni rano."),
        para("Przed sezonem warto sprawdzić stan bieżnika i ciśnienie powietrza w każdym kole auta."),
    ], content_links=["example.pl/geometria/"])
    pressure = page("https://example.pl/cisnienie/", "Pompowanie kół", "x", blocks=[
        para("Ciśnienie powietrza sprawdzaj kompresorem na stacji co miesiąc, zawsze przy zimnych kołach."),
    ])
    geometry = page("https://example.pl/geometria/", "Ustawienie zbieżności", "x", blocks=[
        para("Geometria zawieszenia wpływa na prowadzenie samochodu i równomierne zużycie bieżnika."),
    ])
    contact = page("https://example.pl/kontakt/", "Kontakt", "x", blocks=[para("Zadzwoń do nas od poniedziałku do piątku.")])
    for p in (hub, pressure, geometry, contact):
        p["text"] = " ".join(b["text"] for b in p["blocks"])
    return [hub, pressure, geometry, contact]


def bag(text: str) -> list[float]:
    """Bag of word stems: texts sharing words are close, like a real embedding of similar text."""
    text = text.split("\nQuery: ", 1)[-1]  # the query instruction carries no meaning here
    v = np.zeros(800)
    for w in content_words(text, "pl"):
        v[zlib.crc32(word_key(w).encode()) % 800] = 1
    return v.tolist()


@pytest.fixture
def api(tmp_path, monkeypatch):
    """Stand-ins for every paid call; records them."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    for k in ("TYPESAFE_API_KEY", "FANOUT_MODEL", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS"):
        monkeypatch.delenv(k, raising=False)
    calls = {"model": [], "embed": [], "anchor": [], "judge": []}

    def embed_post(url, body, retries=4, timeout=60):
        calls["embed"].append(body["input"])
        return {"data": [{"index": i, "embedding": bag(t)} for i, t in enumerate(body["input"])], "usage": {"cost": 0}}

    def ask_model(text):
        calls["model"].append(text)
        return SUBQUERIES, 0.001

    def choose(state, qs):
        calls["anchor"].append((state, qs))
        return {"answers": {"fraza": {"choice": "f1", "probabilities": {"f1": 0.8}}}}

    def judge(state, lang):
        calls["judge"].append(state)
        return ANSWER

    monkeypatch.setattr(embeddings, "post", embed_post)
    monkeypatch.setattr(fanout, "ask_model", ask_model)
    monkeypatch.setattr(pairs, "choose_anchor", choose)
    monkeypatch.setattr(pairs, "judge", judge)
    return calls


def args(**kw) -> argparse.Namespace:
    base = dict(sources=None, targets=None, sources_file=None, targets_file=None, fanout=True, semantic=False,
                gsc=None, fanout_pages=None, fanout_pages_file=None, fanout_max=100, workers=2)
    return argparse.Namespace(**{**base, **kw})


# ---- GSC import -------------------------------------------------------------------------------

@pytest.mark.parametrize("content", [
    "Page,Query,Clicks,Impressions,CTR,Position\n"            # Search Analytics for Sheets
    "https://example.pl/opony/,opony zimowe kiedy,10,200,5%,3.1\n"
    "https://example.pl/opony/,wymiana opon cena,2,500,0.4%,8\n",
    "﻿Strona;Zapytanie;Kliknięcia;Wyświetlenia\n"         # Polish headers, semicolons, BOM
    "/opony/;opony zimowe kiedy;10;200\n/opony/;wymiana opon cena;2;\"1 000\"\n",
    "url,query,clicks,impressions\n"                          # BigQuery: one row per day, summed
    "https://www.example.pl/opony/,opony zimowe kiedy,4,100\nhttps://www.example.pl/opony/,opony zimowe kiedy,6,100\n"
    "https://www.example.pl/opony/,wymiana opon cena,2,500\n",
])
def test_gsc_exports_in_any_common_shape(tmp_path, content):
    path = tmp_path / "gsc.csv"
    path.write_text(content, encoding="utf-8")
    gsc = fanout.load_gsc(str(path), "example.pl")
    queries = gsc["example.pl/opony/"]
    assert queries[0]["query"] == "wymiana opon cena"            # most impressions first
    assert {q["query"]: q["clicks"] for q in queries} == {"wymiana opon cena": 2, "opony zimowe kiedy": 10}
    assert queries[1]["impressions"] == 200


def test_a_gsc_file_without_a_query_column_says_what_it_needs(tmp_path):
    path = tmp_path / "gsc.csv"
    path.write_text("Top pages,Clicks,Impressions\nhttps://example.pl/,1,2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no query column - expected one named e.g. Query"):
        fanout.load_gsc(str(path), "example.pl")


def test_gsc_queries_come_first_and_close_generated_ones_merge_into_them():
    vec = {q: np.asarray(bag(q)) / np.linalg.norm(bag(q)) for q in
           ("opony zimowe kiedy", "kiedy opony zimowe", "ciśnienie powietrza kompresor", "opony zimowe kiedy zmienić")}
    gsc = [{"query": "opony zimowe kiedy", "clicks": 10, "impressions": 200},
           {"query": "kiedy opony zimowe", "clicks": 1, "impressions": 50}]
    ai = [{"query": "opony zimowe kiedy zmienić", "type": "how-to"},
          {"query": "ciśnienie powietrza kompresor", "type": "related"}]
    merged = fanout.merge(gsc, ai, vec, 0.8)
    assert [(m["query"], m["source"]) for m in merged] == [("opony zimowe kiedy", "gsc+ai"),
                                                         ("ciśnienie powietrza kompresor", "ai")]
    assert merged[0]["impressions"] == 250 and merged[0]["type"] == "how-to"


# ---- the model answer -------------------------------------------------------------------------

def test_the_model_answer_is_validated():
    good = json.dumps({"subqueries": [{"query": f"pytanie {i}", "type": "how-to"} for i in range(12)]})
    assert len(fanout.parse_subqueries("```json\n" + good + "\n```")) == 12
    odd = json.dumps({"subqueries": [{"query": f"pytanie {i}", "type": "weird"} for i in range(12)]})
    assert {q["type"] for q in fanout.parse_subqueries(odd)} == {"related"}
    for bad in ("Here are your queries: ...", json.dumps({"subqueries": "none"}),
                json.dumps({"subqueries": [{"query": "jedno"}]}), json.dumps({"subqueries": [{"q": 1}] * 12})):
        with pytest.raises(client.JevError):
            fanout.parse_subqueries(bad)


def test_a_malformed_model_answer_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(fanout, "post", lambda url, body: {"choices": [{"message": {"content": "sorry, no JSON"}}]})
    site = pipeline.prepare(tyre_site(), CFG)
    with pytest.raises(RuntimeError, match="fan-out model failed on 1 of 1 hubs"):
        fanout.subqueries([0], site, tmp_path, workers=1)
    assert read_jsonl(tmp_path / "fanout.jsonl") == []


def test_a_bad_key_stops_the_model_calls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    def refused(text):
        raise client.JevError("API error 401 (missing or invalid key)", 401)
    monkeypatch.setattr(fanout, "ask_model", refused)
    site = pipeline.prepare(tyre_site(), CFG)
    with pytest.raises(client.FatalApiError, match="stopped before sending the remaining hubs"):
        fanout.subqueries([0, 1, 2], site, tmp_path, workers=1)


def test_the_request_asks_for_json_without_reasoning(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("FANOUT_MODEL", raising=False)
    sent = []
    content = json.dumps({"subqueries": SUBQUERIES})
    monkeypatch.setattr(fanout, "post", lambda url, body: sent.append((url, body)) or
                        {"choices": [{"message": {"content": content}}], "usage": {"cost": 0.0007}})
    items, cost = fanout.ask_model("{}")
    url, body = sent[0]
    assert url.endswith("/chat/completions") and body["model"] == "deepseek/deepseek-v4-pro"
    assert body["response_format"]["type"] == "json_schema" and body["reasoning"] == {"enabled": False}
    assert body["provider"] == {"sort": "price", "require_parameters": True}
    assert len(items) == 5 and cost == 0.0007


def test_a_model_that_requires_reasoning_gets_the_least(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("FANOUT_MODEL", "google/gemini-3.8-flash")
    monkeypatch.setattr(fanout, "REASONING_REQUIRED", set())
    sent = []

    def post(url, body):
        sent.append(body["reasoning"])
        if body["reasoning"] == {"enabled": False}:
            raise client.JevError("API error 400\nReasoning is mandatory for this endpoint and cannot be disabled.", 400)
        return {"choices": [{"message": {"content": json.dumps({"subqueries": SUBQUERIES})}}]}
    monkeypatch.setattr(fanout, "post", post)
    fanout.ask_model("{}")
    fanout.ask_model("{}")
    assert sent == [{"enabled": False}, {"effort": "minimal"}, {"effort": "minimal"}]


# ---- coverage and candidates ------------------------------------------------------------------

def test_every_subquery_gets_a_state_and_the_unlinked_answer_becomes_a_candidate(api, tmp_path):
    rows, fan, cost = fanout.run(tyre_site(), CFG, args(fanout_pages="^/opony/"), tmp_path, "example.pl", set())
    states = {it["query"]: it["state"] for it in fan["items"]}
    assert states == {"kiedy zmienić opony na zimowe": "hub", "ciśnienie powietrza kompresor": "unlinked",
                      "geometria zawieszenia prowadzenie": "linked", "cena wulkanizacji mobilnej": "gap",
                      "przechowywanie kół sezonowe": "gap"}
    assert [(r["source_url"], r["target_url"], r["anchor_type"]) for r in rows] == [
        ("https://example.pl/opony/", "https://example.pl/cisnienie/", "fanout")]
    r = rows[0]
    # the link goes in the hub paragraph closest to the sub-query, on a phrase Jev picked from it
    assert "ciśnienie powietrza" in r["context"] and r["anchor"] in r["context"]
    assert r["subquery"] == "ciśnienie powietrza kompresor" and r["passage_similarity"] >= 0.4
    phrases = list(api["anchor"][0][1]["fraza"]["criteria"].values())
    assert phrases[0] == "„ciśnienie powietrza”" and "„Żadna" not in phrases[0]
    assert len(api["model"]) == 1


def test_fanout_pairs_go_through_jev_thresholds_and_limits(api, tmp_path):
    rows, fan, _ = fanout.run(tyre_site(), CFG, args(fanout_pages="^/opony/"), tmp_path, "example.pl", set())
    pipeline.judge(rows, tmp_path, workers=1)
    pipeline.select(rows, CFG, 3)
    assert rows[0]["decision"] == "accept" and len(api["judge"]) == 1
    fanout.write_results(tmp_path, fan, rows)
    written = {r["subquery"]: r for r in read_csv(tmp_path / "fanout.csv")}
    assert written["ciśnienie powietrza kompresor"]["link"] == "accepted"
    assert {r["subquery"] for r in read_csv(tmp_path / "fanout_gaps.csv")} == {"cena wulkanizacji mobilnej",
                                                                          "przechowywanie kół sezonowe"}
    data = fanout.load(tmp_path)
    assert data["hubs"][0]["u"] == "https://example.pl/opony/" and len(data["rows"]) == 5
    # a stricter threshold rejects it like any other pair
    pipeline.select(rows, {**CFG, "score_min": 0.99}, 3)
    assert rows[0]["decision_reason"] == "low_score"


def test_a_pair_already_found_by_its_name_is_not_proposed_again(api, tmp_path):
    existing = {("https://example.pl/opony/", "https://example.pl/cisnienie/")}
    rows, fan, _ = fanout.run(tyre_site(), CFG, args(fanout_pages="^/opony/"), tmp_path, "example.pl", existing)
    assert rows == []
    assert next(it for it in fan["items"] if it["state"] == "unlinked")["link"] == "phrase match"


def test_no_fitting_link_text_drops_the_candidate(api, tmp_path, monkeypatch):
    monkeypatch.setattr(pairs, "choose_anchor", lambda state, qs: {
        "answers": {"fraza": {"choice": "none", "probabilities": {"none": 0.7}}}})
    rows, fan, _ = fanout.run(tyre_site(), CFG, args(fanout_pages="^/opony/"), tmp_path, "example.pl", set())
    assert rows == []
    assert next(it for it in fan["items"] if it["state"] == "unlinked")["link"] == "no fitting link text"


def test_a_second_run_pays_for_nothing(api, tmp_path):
    fanout.run(tyre_site(), CFG, args(fanout_pages="^/opony/"), tmp_path, "example.pl", set())
    before = {k: len(v) for k, v in api.items()}
    rows, _, cost = fanout.run(tyre_site(), CFG, args(fanout_pages="^/opony/"), tmp_path, "example.pl", set())
    assert {k: len(v) for k, v in api.items()} == before and len(rows) == 1 and cost == 0


def test_hubs_are_the_best_linked_content_pages_or_the_chosen_ones(tmp_path):
    pages = tyre_site()
    pages[2]["content_links"] = ["example.pl/cisnienie/"]
    pages[3]["content_links"] = ["example.pl/cisnienie/", "example.pl/geometria/"]
    site = pipeline.prepare(pages, CFG)
    assert [site["pages"][i]["url"] for i in fanout.pick_hubs(site, max_n=2)] == [
        "https://example.pl/cisnienie/", "https://example.pl/geometria/"]
    assert fanout.pick_hubs(site, pattern="kontakt") == [3]
    assert fanout.pick_hubs(site, url_list={"example.pl/opony/"}) == [0]
    listing = pipeline.prepare([{**p, "listing": i == 1} for i, p in enumerate(tyre_site())], CFG)
    assert 1 not in fanout.pick_hubs(listing)


def test_semantic_links_pages_from_text_that_does_not_name_them(api, tmp_path):
    pages = tyre_site()
    pages[1]["title"] = pages[1]["h1"] = "Ciśnienie powietrza"   # the hub mentions it in passing
    rows, _, _ = fanout.run(pages, {**CFG, "semantic_min": 0.35}, args(fanout=False, semantic=True), tmp_path,
                            "example.pl", set())
    assert [(r["source_url"], r["target_url"], r["anchor_type"], r["subquery"]) for r in rows] == [
        ("https://example.pl/opony/", "https://example.pl/cisnienie/", "semantic", "")]
    assert rows[0]["anchor"] == "ciśnienie powietrza"
    assert api["model"] == []  # no sub-queries in semantic mode


# ---- passages and phrases ---------------------------------------------------------------------

def test_passages_skip_headings_and_short_blocks_and_carry_their_section():
    p = page("https://example.pl/a/", "Opony", "x", blocks=[
        {"tag": "h2", "text": "Sezon zimowy", "raw": "Sezon zimowy"}, para("Za krótki akapit."),
        para("Opony zimowe trzymają się drogi na śniegu, lodzie i mokrej nawierzchni znacznie lepiej.")])
    found = passages.passages([p])
    assert [x["block"] for x in found] == [2]
    assert found[0]["text"].split("\n")[:2] == ["Opony", "Sezon zimowy"]
    assert passages.query_text("opony").startswith("Instruct: ") and passages.query_text("opony").endswith("\nQuery: opony")


def test_phrase_candidates_stop_at_brackets_links_conjunctions_and_sentence_ends():
    text = f"Sprawdź ciśnienie powietrza (raz w miesiącu) oraz bieżnik opon {LINK_MARK} serwis klimatyzacji. Potem jedź."
    found = passages.phrase_candidates(text, "pl", "ciśnienie powietrza w kole")
    assert found[0] == "ciśnienie powietrza"
    assert all("(" not in f and LINK_MARK not in f and " oraz " not in f and "Potem" not in f for f in found)
    assert all(2 <= len(f.split()) <= 6 for f in found)
    every = passages.phrase_candidates(text, "pl", "nic wspólnego")
    assert "serwis klimatyzacji" in every and len(every) <= passages.MAX_PHRASES


# ---- the command ------------------------------------------------------------------------------

def saved_site(tmp_path):
    ddir = tmp_path / "data" / "example.pl"
    ddir.mkdir(parents=True)
    write_jsonl(ddir / "pages.jsonl", tyre_site())
    (tmp_path / "config.json").write_text(json.dumps({"min_words": 0, "fanout_answer_min": 0.4}), encoding="utf-8")
    return ddir


def test_dry_run_counts_the_cost_without_calling_anything(api, tmp_path, capsys):
    saved_site(tmp_path)
    cli.main(["--data-dir", "data", "run", "--domain", "example.pl", "--no-crawl", "--dry-run", "--fanout"])
    out = capsys.readouterr().out
    assert "fan-out: 4 hubs, 4 to ask deepseek/deepseek-v4-pro" in out and "passages to embed: 4 of 4" in out
    assert all(not v for v in api.values())


def test_run_with_fanout_writes_the_results(api, tmp_path, capsys):
    ddir = saved_site(tmp_path)
    cli.main(["--data-dir", "data", "run", "--domain", "example.pl", "--no-crawl", "--fanout",
              "--fanout-pages", "^/opony/"])
    recs = read_csv(ddir / "recommendations.csv")
    fan = [r for r in recs if r["anchor_type"] == "fanout"]
    assert len(fan) == 1 and fan[0]["subquery"] and fan[0]["passage_similarity"]
    assert (ddir / "fanout.csv").exists() and (ddir / "fanout_gaps.csv").exists()
    assert "Fan-out" in (ddir / "report.html").read_text(encoding="utf-8")
    # a run without --fanout leaves no stale fan-out results behind
    cli.main(["--data-dir", "data", "run", "--domain", "example.pl", "--no-crawl"])
    assert not (ddir / "fanout.json").exists()


def test_fanout_without_a_key_ends_with_a_message(api, tmp_path, capsys, monkeypatch):
    saved_site(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(SystemExit) as stop:
        cli.main(["--data-dir", "data", "run", "--domain", "example.pl", "--no-crawl", "--fanout"])
    out = capsys.readouterr()
    assert stop.value.code == 1 and "OPENROUTER_API_KEY is not set" in out.out + out.err
    assert "Traceback" not in out.out + out.err


@pytest.mark.parametrize("extra, message", [
    (["--no-embeddings"], "drop --no-embeddings"),
    (["--fanout-pages", "(bad"], "--fanout-pages: not a valid regular expression"),
])
def test_fanout_option_mistakes(api, tmp_path, capsys, extra, message):
    saved_site(tmp_path)
    with pytest.raises(SystemExit) as stop:
        cli.main(["--data-dir", "data", "run", "--domain", "example.pl", "--no-crawl", "--fanout", *extra])
    out = capsys.readouterr()
    assert stop.value.code == 1 and message in out.out + out.err


def test_link_text_names_a_topic_not_an_action():
    text = "Google potrafi rozróżnić naturalne linki od nabytych. Zasięg vs. cytowania to inna sprawa, a nagłówki H1 porządkują treść."
    found = passages.phrase_candidates(text, "pl", "nienaturalne linki przychodzące zasięg nagłówki H1")
    assert not any(f.lower().startswith("rozróżnić") or f.endswith("rozróżnić") for f in found)  # no infinitive at an edge
    assert "Zasięg vs" not in found                                                           # no short word at an edge
    assert "naturalne linki" in found and "nagłówki H1" in found                              # acronyms are fine


def test_hint_words_count_in_any_inflected_form_but_not_by_a_shared_stem():
    text = "Strategiczne podejście do schema łączy encje. Strategia treści opiera się na klastrach."
    found = passages.phrase_candidates(text, "pl", "Strategia SEO na 2026")
    assert "Strategiczne podejście" not in found  # "strategiczne" is not a form of "strategia"


def test_pairs_found_by_meaning_meet_a_stricter_score():
    row = {"jev_cannibalisation": 0.1, "jev_context": 0.9, "jev_anchor": 0.9, "score": 0.93, "jev_verdict": "ok"}
    cfg = {**CFG, "score_min": 0.9, "meaning_score_min": 0.95}
    assert pairs.reject_reason({**row, "anchor_type": "partial"}, cfg) == ""
    assert pairs.reject_reason({**row, "anchor_type": "fanout"}, cfg) == "low_score"
    assert pairs.reject_reason({**row, "anchor_type": "semantic", "score": 0.96}, cfg) == ""
