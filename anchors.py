"""Szukanie w treści źródła frazy, na którą można wstawić link do danego celu.

Słowa porównujemy z uwzględnieniem odmiany: pasują, jeśli mają wspólny początek długości
co najmniej 4 i po nim najwyżej 3 litery końcówki, więc
"wymianie opon zimowych" pasuje do celu "Wymiana opon zimowych".

- exact:   fraza pokrywa wszystkie słowa kluczowe celu (z tytułu, h1 albo sluga)
- partial: fraza zawiera co najmniej 2 słowa celu, w tym jedno charakterystyczne
Pojedyncze słowa ("usług", "kontakt") nie są anchorami. Tekst istniejących linków jest w treści
zastąpiony separatorem (crawl.py → blocks), więc fraza nigdy go nie obejmuje. Nie linkujemy
z nagłówków ani nagłówków tabel.
"""
import re
import unicodedata
from collections import Counter
from urllib.parse import urlsplit

STOPWORDS = set("""a aby ale bez by być co czy dla do i ich jak jaki jest jej jego już
ku lub ma może na nad nie o od oraz po pod przez przy się są ta tak te to tu w we z za ze
że czym jakie która który które twoja twojej twoje moje mój dlaczego warto nadal mam kiedy
gdzie ile jaka jakich twój czyli bardzo można
poradnik poradniku przewodnik przewodnika kompletny kompletna kompletne praktyczny praktyczna
praktyczne praktyce wdrożenie wdrożenia krok kroku roku sposób sposoby najlepsze lista powodów
powody""".split())  # ogólniki i wypełniacze tytułów blogowych
MAX_WORDS = 6
MAX_GAP = 2                # maks. słów pomocniczych z rzędu wewnątrz frazy
COMMON_SHARE = 0.8
TITLE_SHARE = 0.1
SENTENCE_END = (".", "!", "?", ";", ":", "…")
PUNCT = ".,;:!?()[]\"'„”«»…"
NO_LINK_BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "th"}


def fold(word: str) -> str:
    word = word.replace("ł", "l")
    return "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))


def words(text: str) -> list[str]:
    return re.findall(r"[0-9a-ząćęłńóśźż]+", text.lower())


def is_filler(word: str) -> bool:
    return word in STOPWORDS or word.isdigit()


def content_words(text: str) -> list[str]:
    return list(dict.fromkeys(fold(w) for w in words(text) if not is_filler(w) and len(w) > 2))


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
        # słowa z wielu tytułów (np. "seo", "google") nie wyróżniają jednego celu
        t = Counter()
        for p in pages:
            t.update({w[:5] for w in content_words(f"{p['title']} {p['h1']}")})
        self.common |= {s for s, n in t.items() if n >= max(3, TITLE_SHARE * len(pages))}

        self._profiles: dict[tuple, tuple] = {}

    def profile(self, target_title: str, target_h1: str, target_url: str) -> tuple:
        """Słowa kluczowe celu (tytuł, h1, slug) - liczone raz na cel."""
        key = (target_title, target_h1, target_url)
        if key not in self._profiles:
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
            self._profiles[key] = (vocab, keyphrases, distinctive)
        return self._profiles[key]

    def find_in_blocks(self, blocks: list[dict], src_title: str, target_title: str, target_h1: str,
                       target_url: str) -> tuple[str, str, int] | None:
        """Najlepsza fraza w blokach treści (exact przed partial, wcześniejszy blok przed późniejszym):
        (fraza, 'exact' | 'partial', indeks bloku) albo None. Nagłówki są pomijane."""
        best = None
        for n, b in enumerate(blocks):
            if b["tag"] in NO_LINK_BLOCKS:
                continue
            found = self.find(b["text"], src_title, target_title, target_h1, target_url)
            if found and (best is None or (found[1] == "exact" and best[1] != "exact")):
                best = (found[0], found[1], n)
                if found[1] == "exact":
                    break
        return best

    def find(self, src_text: str, src_title: str, target_title: str, target_h1: str,
             target_url: str) -> tuple[str, str] | None:
        """Zwraca (fraza, 'exact' | 'partial') albo None, jeśli w tekście nie ma pasującej frazy."""
        vocab, keyphrases, distinctive = self.profile(target_title, target_h1, target_url)
        if not vocab:
            return None
        own = content_words(src_title)

        def match(word: str) -> int | None:
            f = fold(word)
            return next((k for k, v in enumerate(vocab) if same_word(v, f)), None)

        tokens = re.findall(r"\S+", src_text)
        norm = [(words(t) or [""])[0] for t in tokens]
        best, best_score = None, None
        for i in range(len(tokens)):
            if is_filler(norm[i]) or match(norm[i]) is None:
                continue
            hits: set[int] = set()
            gap = 0
            for j in range(i, min(i + MAX_WORDS, len(tokens))):
                w = norm[j]
                if not w:
                    break
                gap = gap + 1 if is_filler(w) else 0
                if gap > MAX_GAP:
                    break
                if not is_filler(w):
                    k = match(w)
                    if k is None:
                        break  # każde słowo treściowe frazy musi pochodzić z celu
                    hits.add(k)
                    enough = len(hits) >= 2
                    # fraza opisująca samą stronę źródłową (np. "rower" na stronie o rowerach
                    # elektrycznych) prowadziłaby do kanibalizacji - pomijamy
                    about_source = all(any(same_word(vocab[h], o) for o in own) for h in hits)
                    exact = any(k <= hits for k in keyphrases)
                    # pełna nazwa celu jest konkretna sama w sobie; partial potrzebuje słowa
                    # charakterystycznego, żeby nie łapać ogólników
                    if enough and (exact or hits & distinctive) and not about_source:
                        score = (exact, len(hits & distinctive), len(hits), -(j - i))
                        if best_score is None or score > best_score:
                            best = (" ".join(tokens[i:j + 1]).strip(PUNCT), "exact" if exact else "partial")
                            best_score = score
                        if exact:
                            break  # nie wydłużamy frazy, która już pokrywa całą nazwę celu
                if tokens[j].endswith(SENTENCE_END + (",",)):
                    break
        return best
