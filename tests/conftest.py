import pytest
import requests
from urllib.parse import urlsplit


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach the internet (a forgotten stub would call a real site or a paid API);
    local servers that tests start themselves are allowed."""
    send = requests.Session.send

    def local_only(self, request, **kw):
        if urlsplit(request.url).hostname not in ("127.0.0.1", "localhost"):
            raise requests.ConnectionError(f"network disabled in tests: {request.url}")
        return send(self, request, **kw)
    monkeypatch.setattr(requests.Session, "send", local_only)


def page(url: str, title: str, text: str, h1: str = "", blocks: list[dict] | None = None, lang: str = "pl",
         content_links: list[str] | None = None, other_links: list[str] | None = None) -> dict:
    """Minimal page record as produced by crawl.extract."""
    return {
        "url": url, "lang": lang, "title": title, "h1": h1 or title, "meta": "", "text": text,
        "text_free": text, "blocks": blocks if blocks is not None else [{"tag": "p", "text": text, "raw": text}],
        "content_links": content_links or [], "breadcrumb_links": [], "other_links": other_links or [],
    }


@pytest.fixture
def site() -> list[dict]:
    filler = " ".join(["Firma remontowa realizuje zlecenia na terenie miasta."] * 25)
    return [
        page("https://example.pl/", "Remonty mieszkań", filler),
        page("https://example.pl/wymiana-opon/", "Wymiana opon zimowych",
             "Oferujemy wymianę opon zimowych i letnich. " + filler),
        page("https://example.pl/geometria/", "Ustawienie geometrii kół",
             "Po wymianie opon zimowych warto sprawdzić ustawienie geometrii kół. " + filler),
        page("https://example.pl/klimatyzacja/", "Serwis klimatyzacji samochodowej",
             "Serwis klimatyzacji samochodowej robimy od ręki. " + filler),
        page("https://example.pl/kontakt/", "Kontakt", "Zadzwoń do nas. " + filler),
    ]
