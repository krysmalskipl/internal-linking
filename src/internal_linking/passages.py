"""Passages: every paragraph of the site as its own embedding, matched to search queries.

Page embeddings (embeddings.py) say what a whole page is about; fan-out and semantic phrases need
to know which paragraph answers a question. Each paragraph, list item, table cell, definition or
quote of at least MIN_WORDS words is embedded with the page title and its section heading in front
(a paragraph alone often lacks its subject), by the same model, in the same way and with the same
cache format as the pages - in data/<domain>/passages.jsonl.

Qwen3 Embedding is trained asymmetrically: documents are embedded as they are, queries with a
one-line task instruction in front. Search queries (fan-out sub-queries, target names) are
embedded as queries, so a query and the paragraph that answers it score high even when they share
few words. Scores are cosines of unit vectors: about 0.7+ for a paragraph that answers the query,
lower for one on a neighbouring topic.
"""
from pathlib import Path

import numpy as np

from . import embeddings
from .anchors import CLOSERS, CONJUNCTIONS, MAX_GAP, OPENERS, PUNCT, SENTENCE_END, fold, is_filler, same_word, words
from .crawl import LINK_MARK
from .jev import pairs

PASSAGE_TAGS = {"p", "li", "td", "dd", "blockquote"}
MIN_WORDS = 8        # shorter blocks (a button label, a one-line list item) answer nothing on their own
MAX_CHARS = 2000
PHRASE_WORDS = (2, 6)
MAX_PHRASES = 8      # phrases offered to Jev per passage
QUERY_TASK = "Given a search query, retrieve a passage of a web page that answers it"
TOPIC_TASK = "Given the title of a web page, retrieve passages of other pages that discuss its topic"
CACHE = "passages.jsonl"


def passages(pages: list[dict]) -> list[dict]:
    """Every passage of every page: {page: page index, block: block index, text: what is embedded}."""
    out = []
    for i, p in enumerate(pages):
        blocks = p.get("blocks", [])
        for b, block in enumerate(blocks):
            raw = block.get("raw") or block["text"]
            if block["tag"] not in PASSAGE_TAGS or len(raw.split()) < MIN_WORDS:
                continue
            section = pairs.surroundings(blocks, b)[0]
            text = "\n".join(x for x in (p.get("title", ""), section, raw) if x)[:MAX_CHARS]
            out.append({"page": i, "block": b, "text": text})
    return out


def query_text(query: str, task: str = QUERY_TASK) -> str:
    """A query as Qwen3 Embedding expects it: the task instruction, then the query."""
    return f"Instruct: {task}\nQuery: {query}"


def embed_passages(found: list[dict], ddir: Path, workers: int = 4) -> tuple[np.ndarray, float]:
    """Unit vectors of the passages (rows in the order given), and the cost of the new ones."""
    vectors, cost = embeddings.embed_texts({str(n): x["text"] for n, x in enumerate(found)}, ddir / CACHE, workers,
                                           unit_name="passages")
    return np.stack([vectors[str(n)] for n in range(len(found))]) if found else np.zeros((0, 1)), cost


def embed_queries(queries: list[str], ddir: Path, workers: int = 4, task: str = QUERY_TASK) -> tuple[np.ndarray, float]:
    """Unit vectors of search queries, embedded with the query instruction (cached with the passages)."""
    texts = {str(n): query_text(q, task) for n, q in enumerate(queries)}
    vectors, cost = embeddings.embed_texts(texts, ddir / CACHE, workers, unit_name="queries")
    return np.stack([vectors[str(n)] for n in range(len(queries))]) if queries else np.zeros((0, 1)), cost


def count_new(found: list[dict], queries: list[str], ddir: Path, task: str = QUERY_TASK) -> int:
    """Passages and queries a run would send to the embeddings API (for --dry-run)."""
    return embeddings.count_new_texts([x["text"] for x in found] + [query_text(q, task) for q in queries], ddir / CACHE)


def best(scores: np.ndarray, mask: np.ndarray) -> tuple[int, float]:
    """(index, score) of the best passage among those in mask; (-1, 0.0) when the mask is empty."""
    if not mask.any():
        return -1, 0.0
    masked = np.where(mask, scores, -np.inf)
    n = int(np.argmax(masked))
    return n, round(float(masked[n]), 3)


def best_per_page(scores: np.ndarray, page_of: np.ndarray, mask: np.ndarray, k: int,
                  minimum: float) -> list[tuple[int, float]]:
    """The k best passages scoring at least `minimum`, at most one per page: [(passage, score)], best first."""
    out, seen = [], set()
    for n in np.argsort(-np.where(mask, scores, -np.inf)):
        if not mask[n] or scores[n] < minimum or len(out) >= k:
            break
        if page_of[n] not in seen:
            seen.add(page_of[n])
            out.append((int(n), round(float(scores[n]), 3)))
    return out


def edge_word(token: str, word: str, lang: str) -> bool:
    """Can a phrase start or end on this word? Not a function word, not a Polish infinitive - a link
    text names a topic, not an action ("rozróżnić naturalne linki" was rejected by hand) - and not a
    short lower-case word ("Zasięg vs"); acronyms and words with digits (AI, H1, n8n) are fine."""
    if not word or is_filler(word, lang):
        return False
    if lang == "pl" and word.endswith("ć"):
        return False
    return len(word) >= 3 or any(c.isdigit() for c in word) or token.strip(PUNCT).isupper()


def phrase_candidates(text: str, lang: str, hint: str) -> list[str]:
    """Phrases of 2-6 words in a passage that could carry a link: they start and end on a word that
    can name a topic (edge_word), never run into a bracket, a quote, an existing link or past a
    sentence end, never span a conjunction ("ChatGPT or Gemini" names two things). Ranked by words
    shared with the hint (the sub-query and the target's name, in any inflected form), at most
    MAX_PHRASES; when some phrase shares a word, only those."""
    hint_words = [fold(w) for w in words(hint) if not is_filler(w, lang)]
    tokens = text.split()
    norm = [(words(t) or [""])[0] for t in tokens]
    found: dict[str, tuple] = {}
    for i in range(len(tokens)):
        if tokens[i] == LINK_MARK or not edge_word(tokens[i], norm[i], lang):
            continue
        gap = 0
        for j in range(i, min(i + PHRASE_WORDS[1], len(tokens))):
            w = norm[j]
            if tokens[j] == LINK_MARK or not w or (j > i and tokens[j].startswith(OPENERS)):
                break
            if j > i and w in CONJUNCTIONS.get(lang, ()):
                break
            gap = gap + 1 if is_filler(w, lang) else 0
            if gap > MAX_GAP:
                break
            if j > i and edge_word(tokens[j], w, lang):
                phrase = " ".join(tokens[i:j + 1]).strip(PUNCT)
                ws = words(phrase)
                if PHRASE_WORDS[0] <= len(ws):
                    hits = sum(any(same_word(fold(x), h) for h in hint_words) for x in ws if not is_filler(x, lang))
                    found.setdefault(phrase.lower(), (-hits, len(ws), i, phrase))
            if tokens[j].endswith(SENTENCE_END + (",",)) or tokens[j].rstrip(".,;:!?…").endswith(CLOSERS):
                break
    ranked = sorted(found.values())
    if any(r[0] < 0 for r in ranked):  # phrases sharing a word with the hint, when there are any
        ranked = [r for r in ranked if r[0] < 0]
    return [r[3] for r in ranked[:MAX_PHRASES]]
