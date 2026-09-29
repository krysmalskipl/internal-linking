"""Finds phrases in a source page that can carry a link to a given target page.

Words are compared inflection-aware (tuned for Polish): they match if they share a prefix of at
least 4 letters followed by an ending of at most 3 letters, so "wymianie opon zimowych" matches
the target "Wymiana opon zimowych".

- exact:   the phrase covers all keywords of the target (from its title, H1 or slug)
- partial: the phrase contains at least 2 target words, including a distinctive one
Single words ("usług", "kontakt") are never anchors. Existing link text is replaced by a separator
in the content blocks (see crawl.py), so a phrase never overlaps it. Headings are never used.
"""
import re
import unicodedata
from collections import Counter
from urllib.parse import urlsplit

# Polish function words plus generic blog-title fillers - never keywords
STOPWORDS = set("""a aby ale bez by być co czy dla do i ich jak jaki jest jej jego już
ku lub ma może na nad nie o od oraz po pod przez przy się są ta tak te to tu w we z za ze
że czym jakie która który które twoja twojej twoje moje mój dlaczego warto nadal mam kiedy
gdzie ile jaka jakich twój czyli bardzo można
poradnik poradniku przewodnik przewodnika kompletny kompletna kompletne praktyczny praktyczna
praktyczne praktyce wdrożenie wdrożenia krok kroku roku sposób sposoby najlepsze lista powodów
powody""".split())
MAX_WORDS = 6
MAX_GAP = 2                # max consecutive function words inside a phrase
COMMON_SHARE = 0.8         # words on more pages than this share distinguish nothing
TITLE_SHARE = 0.1          # words in this share of titles (min. 3) are not distinctive
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
    """Same word in another inflected form: shared prefix ≥ 4, endings of at most 3 letters."""
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
        # words (first 5 letters) present on almost every page (site name, author byline) do not
        # distinguish any target; service keywords may appear on half the pages and must stay
        c = Counter()
        for p in pages:
            c.update({fold(w)[:5] for w in words(p["text"])})
        self.common = {s for s, n in c.items() if n > COMMON_SHARE * len(pages)}
        # words from many titles (e.g. "seo", "google") do not single out one target
        t = Counter()
        for p in pages:
            t.update({w[:5] for w in content_words(f"{p['title']} {p['h1']}")})
        self.common |= {s for s, n in t.items() if n >= max(3, TITLE_SHARE * len(pages))}
        self._profiles: dict[tuple, tuple] = {}

    def profile(self, target_title: str, target_h1: str, target_url: str) -> tuple:
        """Target keywords (title, H1, slug) - computed once per target."""
        key = (target_title, target_h1, target_url)
        if key not in self._profiles:
            slug = urlsplit(target_url).path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
            vocab: list[str] = []            # target keywords
            keyphrases: list[set[int]] = []  # title / H1 / slug as sets of indices into vocab
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
        """Best phrase across content blocks (exact before partial, earlier block first):
        (phrase, 'exact' | 'partial', block index) or None. Headings are skipped."""
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
        """(phrase, 'exact' | 'partial') or None when the text has no matching phrase."""
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
                        break  # every content word of the phrase must come from the target
                    hits.add(k)
                    enough = len(hits) >= 2
                    # a phrase describing the source page itself (e.g. "bikes" on a page about
                    # e-bikes) would cannibalise it - skip
                    about_source = all(any(same_word(vocab[h], o) for o in own) for h in hits)
                    # the longest target name (title, H1 or slug) fully covered by the phrase
                    covered = max((k for k in keyphrases if k <= hits), key=len, default=set())
                    exact = bool(covered)
                    # a target's full name is specific on its own; a partial match needs a
                    # distinctive word so generic phrases are not picked up
                    if enough and (exact or hits & distinctive) and not about_source:
                        # longest covered name first, then no words beyond it
                        # ("Google Search Console", not "Google Search Console using")
                        extra = len(hits - covered) if exact else 0
                        score = (len(covered), -extra, len(hits & distinctive), len(hits), -(j - i))
                        if best_score is None or score > best_score:
                            best = (" ".join(tokens[i:j + 1]).strip(PUNCT), "exact" if exact else "partial")
                            best_score = score
                if tokens[j].endswith(SENTENCE_END + (",",)):
                    break
        return best
