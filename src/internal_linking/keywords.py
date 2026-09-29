"""Keyword mode: link only the phrases you choose.

The keywords file is a CSV with a header - `keyword` is required, `target_url` and `match` are
optional:

    keyword,target_url,match
    korty do squasha,https://example.pl/korty-do-squasha/,exact
    treningi grupowe squash,,partial

A plain text file with one keyword per line works too. `match`: `exact` (the whole keyword,
inflection-aware) or `partial` (at least 2 of its words; the default). Without `target_url` the
target is the page whose title, H1 or slug matches the keyword best.
"""
import csv
from pathlib import Path

from .anchors import content_words, same_word
from .common import norm_url

MATCH_TYPES = {"exact", "partial"}


def load_keywords(path: str | Path) -> list[dict]:
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    first = text.splitlines()[0].lower() if text.strip() else ""
    if "keyword" in first and ("," in first or ";" in first):
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;")
        rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
    else:
        rows = [{"keyword": ln} for ln in text.splitlines()]
    out = []
    for r in rows:
        keyword = (r.get("keyword") or "").strip()
        if not keyword or keyword.startswith("#"):
            continue
        match = (r.get("match") or "partial").strip().lower()
        if match not in MATCH_TYPES:
            raise SystemExit(f"{path}: unknown match type '{match}' for '{keyword}' (use exact or partial)")
        out.append({"keyword": keyword, "target_url": (r.get("target_url") or "").strip(), "match": match})
    if not out:
        raise SystemExit(f"{path}: no keywords found")
    return out


def _coverage(keyword_words: list[str], name_words: list[str]) -> tuple[float, float]:
    """(share of keyword words found in the name, share of name words that are keyword words)."""
    if not keyword_words or not name_words:
        return 0.0, 0.0
    hit = sum(any(same_word(k, n) for n in name_words) for k in keyword_words)
    back = sum(any(same_word(n, k) for k in keyword_words) for n in name_words)
    return hit / len(keyword_words), back / len(name_words)


def resolve_target(keyword: str, pages: list[dict], titles: list[str], lang: str | None = None) -> int | None:
    """Index of the page that best matches the keyword by title, H1 or slug; None if no page covers
    at least half of the keyword's words."""
    best, best_key = None, None
    for i, p in enumerate(pages):
        if lang and p["lang"] != lang:
            continue
        kw = content_words(keyword, p["lang"])
        slug = p["url"].rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        for name in (titles[i], p["h1"], slug):
            cover, precision = _coverage(kw, content_words(name, p["lang"]))
            key = (cover, precision)
            if cover >= 0.5 and (best_key is None or key > best_key):
                best, best_key = i, key
    return best


def resolve(keywords: list[dict], pages: list[dict], titles: list[str]) -> tuple[list[dict], list[str]]:
    """Attaches the target page index to every keyword; returns (resolved, warnings)."""
    by_url = {norm_url(p["url"]): i for i, p in enumerate(pages)}
    resolved, warnings = [], []
    for k in keywords:
        if k["target_url"]:
            j = by_url.get(norm_url(k["target_url"]))
            if j is None:
                warnings.append(f"'{k['keyword']}': target {k['target_url']} is not among the crawled pages")
                continue
            resolved.append({**k, "target": j, "auto_target": False})
        else:
            j = resolve_target(k["keyword"], pages, titles)
            if j is None:
                warnings.append(f"'{k['keyword']}': no page matches it - add target_url")
                continue
            resolved.append({**k, "target": j, "target_url": pages[j]["url"], "auto_target": True})
    return resolved, warnings
