"""Judges a single (source page, phrase, target) pair with Jev - "jev mode".

Modelled on relevance-coarse-filter from newsjack: instead of one "pick the best target from the
list" question per page (probabilities then spread across all options), every pair gets its own
call with a few closed questions. The block type (paragraph, list...) is computed from the HTML
and passed as a fact. Hard rules on the answers (thresholds in config.json) produce a reason code.

Questions exist in Polish and English; each pair is asked in the language of its pages.
"""
import hashlib
import json

from .client import decide

QUESTIONS = {
    "kontekst": {
        "type": "noul",
        "instructions": "Czy fragment tekstu, w którym stoi fraza linku, omawia zagadnienie, "
                        "które strona docelowa rozwija? Weź pod uwagę nagłówek sekcji i sąsiednie fragmenty.",
        "criteria": {
            "true": "Tak - czytelnik po kliknięciu dostanie rozwinięcie tego, o czym właśnie czyta.",
            "false": "Nie - fragment mówi o czymś innym, a słowa frazy tylko powierzchownie zgadzają się "
                     "z tytułem celu (np. 'cena wymiany opon' w zdaniu o terminach wizyt, gdy cel to "
                     "poradnik techniczny o oponach). W razie wątpliwości wybierz nie.",
        },
    },
    "anchor": {
        "type": "noul",
        "instructions": "Czy fraza jest naturalnym tekstem linku: zrozumiała sama w sobie i jasno "
                        "zapowiada, o czym jest strona docelowa?",
        "criteria": {
            "true": "Tak - np. 'plik robots.txt', 'audyt SEO sklepu', 'wymiana opon zimowych'.",
            "false": "Nie - fraza urwana, zbyt ogólna albo z przypadkowymi słowami "
                     "(np. 'robots.txt nie blokuje', 'praktyczne wdrożenie', 'sprawdzić Google').",
        },
    },
    "wartosc": {
        "type": "score",
        "instructions": "Jak wartościowy dla czytelnika byłby link na tej frazie do strony docelowej, "
                        "w tym konkretnym miejscu tekstu?",
        "criteria": [
            "Bezwartościowy - link myli czytelnika albo prowadzi w inną stronę niż zdanie",
            "Słaby - luźny związek, czytelnik raczej nie kliknie",
            "Poprawny - związany temat, umiarkowanie przydatny",
            "Dobry - rozwija wątek z tego zdania",
            "Idealny - czytelnik w tym miejscu szuka dokładnie tej strony",
        ],
    },
    "kanibalizacja": {
        "type": "noul",
        "instructions": "Czy fraza linku opisuje przede wszystkim główny temat strony źródłowej "
                        "(a nie strony docelowej)?",
        "criteria": {
            "true": "Fraza to temat samej strony źródłowej - link odbierałby jej ruch na tę frazę.",
            "false": "Fraza to temat poboczny względem strony źródłowej, rozwijany przez cel.",
        },
    },
    "powod": {
        "type": "choice",
        "instructions": "Jaka jest główna ocena tej propozycji linku?",
        "criteria": {
            "ok": "Dobry link - temat zdania i celu się zgadza, fraza pasuje",
            "temat_niezwiazany": "Fraza pasuje słownie, ale zdanie dotyczy innego tematu niż cel",
            "fraza_ogolna": "Fraza jest zbyt ogólna, żeby jednoznacznie wskazywać ten cel",
            "kanibalizacja": "Fraza opisuje temat strony źródłowej",
            "inna_intencja": "Czytelnik w tym miejscu szuka innego typu strony (np. usługi zamiast artykułu)",
            "nawigacja_szablon": "Fraza stoi w liście, nawigacji albo powtarzalnym bloku",
        },
    },
}


PLACES = {"p": "akapit", "li": "punkt listy", "td": "komórka tabeli", "dd": "definicja",
          "dt": "termin", "blockquote": "cytat", "figcaption": "podpis grafiki", "inne": "inny fragment"}

QUESTIONS_EN = {
    "context": {
        "type": "noul",
        "instructions": "Does the passage containing the link phrase discuss the subject that the target page "
                        "covers? Take the section heading and the neighbouring passages into account.",
        "criteria": {
            "true": "Yes - after clicking, the reader gets more on exactly what they are reading about.",
            "false": "No - the passage is about something else and the phrase only superficially matches the "
                     "target's title (e.g. 'tyre change price' in a sentence about opening hours, when the "
                     "target is a technical guide to tyres). When in doubt, answer no.",
        },
    },
    "anchor": {
        "type": "noul",
        "instructions": "Is the phrase natural link text: understandable on its own and clearly announcing "
                        "what the target page is about?",
        "criteria": {
            "true": "Yes - e.g. 'robots.txt file', 'SEO audit for online stores', 'winter tyre change'.",
            "false": "No - truncated, too generic or with stray words "
                     "(e.g. 'robots.txt does not block', 'practical implementation', 'check Google').",
        },
    },
    "value": {
        "type": "score",
        "instructions": "How valuable to the reader would a link on this phrase to the target page be, "
                        "at this exact spot in the text?",
        "criteria": [
            "Worthless - the link confuses the reader or leads away from the sentence",
            "Weak - loose connection, the reader will hardly click",
            "Fair - related subject, moderately useful",
            "Good - expands on the point made in this sentence",
            "Ideal - at this spot the reader is looking for exactly this page",
        ],
    },
    "cannibalisation": {
        "type": "noul",
        "instructions": "Does the link phrase mainly describe the main topic of the source page "
                        "(rather than the target page)?",
        "criteria": {
            "true": "The phrase is the source page's own topic - a link would take its traffic for that phrase.",
            "false": "The phrase is a side topic of the source page that the target page expands on.",
        },
    },
    "verdict": {
        "type": "choice",
        "instructions": "What is the overall verdict on this link suggestion?",
        "criteria": {
            "ok": "Good link - the passage and the target share the topic and the phrase fits",
            "off_topic": "The phrase matches the words, but the passage is about a different topic than the target",
            "too_generic": "The phrase is too generic to point unambiguously to this target",
            "cannibalisation": "The phrase describes the source page's own topic",
            "wrong_intent": "At this spot the reader expects a different kind of page (e.g. a service, not an article)",
            "navigation_or_template": "The phrase sits in a list, navigation or a repeated block",
        },
    },
}
PLACES_EN = {"p": "paragraph", "li": "list item", "td": "table cell", "dd": "definition", "dt": "term",
             "blockquote": "quote", "figcaption": "image caption", "inne": "other passage"}
CONTEXT_CHARS = 600
def context(block: dict, phrase: str) -> str:
    """The block (paragraph, list item) holding the phrase, trimmed to ~600 characters around it."""
    raw = block.get("raw") or block["text"]
    i = raw.lower().find(phrase.lower())
    if i < 0 or len(raw) <= CONTEXT_CHARS:
        return raw[:CONTEXT_CHARS * 2]
    start = max(0, i - CONTEXT_CHARS // 2)
    end = min(len(raw), i + len(phrase) + CONTEXT_CHARS // 2)
    return ("…" if start else "") + raw[start:end] + ("…" if end < len(raw) else "")


NEIGHBOUR_CHARS = 300
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def surroundings(blocks: list[dict], b: int) -> tuple[str, str, str]:
    """Section heading above block b, and the end of the previous / start of the next passage."""
    section = next((x["raw"] for x in reversed(blocks[:b]) if x["tag"] in HEADINGS), "")
    prev = next((x.get("raw") or x["text"] for x in reversed(blocks[:b]) if x["tag"] not in HEADINGS), "")
    nxt = next((x.get("raw") or x["text"] for x in blocks[b + 1:] if x["tag"] not in HEADINGS), "")
    return section, prev[-NEIGHBOUR_CHARS:], nxt[:NEIGHBOUR_CHARS]


def build_state(src: dict, src_title: str, blocks: list[dict], b: int, target: dict, target_title: str,
                phrase: str, lang: str = "pl") -> dict:
    """What Jev sees for one pair: the source page, the section, the passage with its neighbours and
    the target page."""
    block = blocks[b]
    section, prev, nxt = surroundings(blocks, b)
    if lang == "en":
        return {
            "source_page": {"title": src_title, "h1": src["h1"], "description": src["meta"][:200],
                            "url": src["url"]},
            "section_heading": section,
            "placement": PLACES_EN.get(block["tag"], "other passage"),
            "previous_passage": prev,
            "passage_with_phrase": context(block, phrase),
            "next_passage": nxt,
            "link_phrase": phrase,
            "target_page": {"title": target_title, "h1": target["h1"], "description": target["meta"][:200],
                            "url": target["url"], "content_start": target["text"][:400]},
        }
    return {
        "strona_zrodlowa": {"tytul": src_title, "h1": src["h1"], "opis": src["meta"][:200], "url": src["url"]},
        "naglowek_sekcji": section,
        "miejsce": PLACES.get(block["tag"], "inny fragment"),
        "poprzedni_fragment": prev,
        "fragment_z_fraza": context(block, phrase),
        "nastepny_fragment": nxt,
        "fraza_linku": phrase,
        "strona_docelowa": {"tytul": target_title, "h1": target["h1"], "opis": target["meta"][:200],
                            "url": target["url"], "poczatek_tresci": target["text"][:400]},
    }


def questions(lang: str = "pl") -> dict:
    return QUESTIONS_EN if lang == "en" else QUESTIONS


def cache_key(state: dict, lang: str = "pl") -> str:
    body = json.dumps({"state": state, "questions": questions(lang)}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(body.encode()).hexdigest()


def judge(state: dict, lang: str = "pl") -> dict:
    return decide(state, questions(lang))


# Jev verdict keys (Polish, part of the questions) → English reason codes in the output
VERDICTS = {"ok": "ok", "temat_niezwiazany": "off_topic", "fraza_ogolna": "too_generic",
            "kanibalizacja": "cannibalisation", "inna_intencja": "wrong_intent",
            "nawigacja_szablon": "navigation_or_template"}
# HTML block tag → placement label in the output
PLACEMENT = {"p": "paragraph", "li": "list item", "td": "table cell", "dd": "definition", "dt": "term",
             "blockquote": "quote", "figcaption": "caption", "inne": "other"}


# answer keys per language: (context, anchor, value, cannibalisation, verdict)
ANSWER_KEYS = {"pl": ("kontekst", "anchor", "wartosc", "kanibalizacja", "powod"),
               "en": ("context", "anchor", "value", "cannibalisation", "verdict")}


def summarize(resp: dict, lang: str = "pl") -> dict:
    """Jev answer → CSV columns. `score` = P(verdict "ok"), the strongest signal on hand labels."""
    a = resp["answers"]
    k_context, k_anchor, k_value, k_cannib, k_verdict = ANSWER_KEYS["en" if lang == "en" else "pl"]
    verdict = a[k_verdict]["choice"]
    return {
        "score": round(a[k_verdict]["probabilities"].get("ok", 0), 3),
        "jev_context": round(a[k_context]["noul"], 3),
        "jev_anchor": round(a[k_anchor]["noul"], 3),
        "jev_value": round(a[k_value]["score"] / 4, 3),  # 0..1
        "jev_cannibalisation": round(a[k_cannib]["noul"], 3),
        "jev_verdict": VERDICTS.get(verdict, verdict),
    }


def reject_reason(row: dict, cfg: dict) -> str:
    """Hard rules on Jev answers (thresholds from config.json); empty string = the pair passes."""
    if row["jev_cannibalisation"] >= cfg["cannibalisation_max"]:
        return "cannibalisation"
    if row["jev_context"] < cfg["context_min"]:
        return "off_topic"
    if row["jev_anchor"] < cfg["anchor_min"]:
        return "weak_anchor"
    if row["score"] < cfg["score_min"]:
        return row["jev_verdict"] if row["jev_verdict"] != "ok" else "low_score"
    return ""


def add_parser(sub) -> None:
    p = sub.add_parser("questions", help="print the questions sent to Jev for every pair")
    p.add_argument("--lang", choices=["pl", "en"], default="en")
    p.set_defaults(func=lambda args: print(json.dumps(questions(args.lang), ensure_ascii=False, indent=2)))
