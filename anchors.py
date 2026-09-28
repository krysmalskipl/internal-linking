"""Szukanie w treści źródła frazy, na którą można wstawić link do danego celu.

Słowa porównujemy z uwzględnieniem odmiany: pasują, jeśli mają wspólny początek długości
co najmniej 4 i po nim najwyżej 3 litery końcówki, więc
"wymianie opon zimowych" pasuje do celu "Wymiana opon zimowych".

- exact:   fraza pokrywa wszystkie słowa kluczowe celu (z tytułu, h1 albo sluga)
- partial: fraza zawiera co najmniej 2 słowa celu, w tym jedno charakterystyczne
Pojedyncze słowa ("usług", "kontakt") nie są anchorami. Tekst istniejących linków jest w treści
zastąpiony separatorem (crawl.py → text_free), więc fraza nigdy go nie obejmuje.
"""
import re
import unicodedata
from collections import Counter
from urllib.parse import urlsplit

STOPWORDS = set("""a aby ale bez by być co czy dla do i ich jak jaki jest jej jego już
ku lub ma może na nad nie o od oraz po pod przez przy się są ta tak te to tu w we z za ze
że czym jakie która który które twoja twojej twoje moje mój dlaczego warto nadal mam kiedy
gdzie ile jaka jakich twój czyli bardzo można""".split())
MAX_WORDS = 6
COMMON_SHARE = 0.8
SENTENCE_END = (".", "!", "?", ";", ":", "…")
PUNCT = ".,;:!?()[]\"'„”«»…"


def fold(word: str) -> str:
    word = word.replace("ł", "l")
    return "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))


def words(text: str) -> list[str]:
    return re.findall(r"[0-9a-ząćęłńóśźż]+", text.lower())


def content_words(text: str) -> list[str]:
    return list(dict.fromkeys(fold(w) for w in words(text) if w not in STOPWORDS and len(w) > 2))


def same_word(a: str, b: str) -> bool:
    """Te same słowa w innej odmianie: wspólny początek ≥ 4, a końcówki najwyżej 3-literowe."""
    if a == b:
        return True
    p = 0
    for x, y in zip(a, b):
        if x != y:
            break
        p += 1
    return p >= 4 and p >= max(len(a), len(b)) - 3


class AnchorFinder:
    def __init__(self, pages: list[dict]):
        # słowa (po 5 literach) obecne prawie na każdej stronie (nazwa serwisu, podpis autora)
        # nie wyróżniają żadnego celu; słowa kluczowe usług bywają na połowie stron i muszą zostać
        c = Counter()
        for p in pages:
            c.update({fold(w)[:5] for w in words(p["text"])})
        self.common = {s for s, n in c.items() if n > COMMON_SHARE * len(pages)}

    def find(self, src_text: str, src_title: str, target_title: str, target_h1: str,
             target_url: str) -> tuple[str, str] | None:
        """Zwraca (fraza, 'exact' | 'partial') albo None, jeśli w tekście nie ma pasującej frazy."""
        slug = urlsplit(target_url).path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        vocab: list[str] = []            # słowa kluczowe celu
        keyphrases: list[set[int]] = []  # tytuł / h1 / slug jako zbiory indeksów w vocab
        for text in (target_title, target_h1, slug):
            idx = set()
            for w in content_words(text):
                k = next((n for n, v in enumerate(vocab) if same_word(v, w)), None)
                if k is None:
                    vocab.append(w)
                    k = len(vocab) - 1
                idx.add(k)
            if idx:
                keyphrases.append(idx)
        distinctive = {k for k, v in enumerate(vocab) if v[:5] not in self.common}
        if not distinctive:
            return None
        own = content_words(src_title)

        def match(word: str) -> int | None:
            f = fold(word)
            return next((k for k, v in enumerate(vocab) if same_word(v, f)), None)

        tokens = re.findall(r"\S+", src_text)
        norm = [(words(t) or [""])[0] for t in tokens]
        best, best_score = None, None
        for i in range(len(tokens)):
            if norm[i] in STOPWORDS or match(norm[i]) is None:
                continue
            hits: set[int] = set()
            for j in range(i, min(i + MAX_WORDS, len(tokens))):
                w = norm[j]
                if not w:
                    break
                if w not in STOPWORDS:
                    k = match(w)
                    if k is None:
                        break  # każde słowo treściowe frazy musi pochodzić z celu
                    hits.add(k)
                    enough = len(hits) >= 2
                    # fraza opisująca samą stronę źródłową (np. "rower" na stronie o rowerach
                    # elektrycznych) prowadziłaby do kanibalizacji - pomijamy
                    about_source = all(any(same_word(vocab[h], o) for o in own) for h in hits)
                    if enough and hits & distinctive and not about_source:
                        exact = any(k <= hits for k in keyphrases)
                        score = (exact, len(hits & distinctive), len(hits), -(j - i))
                        if best_score is None or score > best_score:
                            best = (" ".join(tokens[i:j + 1]).strip(PUNCT), "exact" if exact else "partial")
                            best_score = score
                if tokens[j].endswith(SENTENCE_END):
                    break
        return best
