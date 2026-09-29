"""Judges a single (source page, phrase, target) pair with Jev - "jev mode".

Modelled on relevance-coarse-filter from newsjack: instead of one "pick the best target from the
list" question per page (probabilities then spread across all options), every pair gets its own
call with a few closed questions. The block type (paragraph, list...) is computed from the HTML
and passed as a fact. Hard rules on the answers (thresholds in config.json) produce a reason code.

The questions are in Polish on purpose: the tool targets Polish-language sites.
"""
import hashlib
import json

from .client import decide

QUESTIONS = {
    "kontekst": {
        "type": "noul",
        "instructions": "Czy fragment tekstu, w którym stoi fraza linku, omawia zagadnienie, "
                        "które strona docelowa rozwija?",
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


def build_state(src: dict, src_title: str, block: dict, target: dict, target_title: str, phrase: str) -> dict:
    return {
        "strona_zrodlowa": {"tytul": src_title, "h1": src["h1"], "url": src["url"]},
        "miejsce": PLACES.get(block["tag"], "inny fragment"),
        "fragment_z_fraza": context(block, phrase),
        "fraza_linku": phrase,
        "strona_docelowa": {"tytul": target_title, "h1": target["h1"], "opis": target["meta"][:200],
                            "url": target["url"], "poczatek_tresci": target["text"][:400]},
    }


def cache_key(state: dict) -> str:
    body = json.dumps({"state": state, "questions": QUESTIONS}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(body.encode()).hexdigest()


def judge(state: dict) -> dict:
    return decide(state, QUESTIONS)


# Jev verdict keys (Polish, part of the questions) → English reason codes in the output
VERDICTS = {"ok": "ok", "temat_niezwiazany": "off_topic", "fraza_ogolna": "too_generic",
            "kanibalizacja": "cannibalisation", "inna_intencja": "wrong_intent",
            "nawigacja_szablon": "navigation_or_template"}
# HTML block tag → placement label in the output
PLACEMENT = {"p": "paragraph", "li": "list item", "td": "table cell", "dd": "definition", "dt": "term",
             "blockquote": "quote", "figcaption": "caption", "inne": "other"}


def summarize(resp: dict) -> dict:
    """Jev answer → CSV columns. `score` = P(verdict "ok"), the strongest signal on hand labels."""
    a = resp["answers"]
    return {
        "score": round(a["powod"]["probabilities"].get("ok", 0), 3),
        "jev_context": round(a["kontekst"]["noul"], 3),
        "jev_anchor": round(a["anchor"]["noul"], 3),
        "jev_value": round(a["wartosc"]["score"] / 4, 3),  # 0..1
        "jev_cannibalisation": round(a["kanibalizacja"]["noul"], 3),
        "jev_verdict": VERDICTS.get(a["powod"]["choice"], a["powod"]["choice"]),
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
    p.set_defaults(func=lambda args: print(json.dumps(QUESTIONS, ensure_ascii=False, indent=2)))
