"""Klient Jev (TypeSafe) przez OpenRouter Decisions API - wersja do wołania w pętli.

Oparty na ~/.claude/skills/jev/jev.py (ten sam endpoint, model i nagłówki), ale rzuca wyjątki
zamiast sys.exit i ponawia zapytanie przy 429 / 5xx.
"""
import os
import time
from pathlib import Path

import requests


def load_dotenv(path: Path = Path(__file__).parent / ".env") -> None:
    """Wczytuje KLUCZ=wartość z .env projektu; zmienne już ustawione w terminalu mają pierwszeństwo."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv()

API_URL = os.environ.get("JEV_API_URL", "https://openrouter.ai/api/alpha/decisions")
MODEL = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
HINTS = {401: "zły lub brak klucza", 402: "brak kredytów na koncie OpenRouter",
         429: "limit zapytań"}


class JevError(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise JevError("Brak OPENROUTER_API_KEY. Wpisz go w pliku .env w katalogu projektu "
                       "(OPENROUTER_API_KEY=sk-or-...).")
    return key


def decide(state: dict, questions: dict, retries: int = 4, timeout: int = 60) -> dict:
    body = {"model": MODEL, "state": state, "questions": questions}
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
               "X-Title": "internal-linking"}
    for attempt in range(retries + 1):
        try:
            r = requests.post(API_URL, json=body, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            if attempt == retries:
                raise JevError(f"Brak połączenia z API: {e}") from e
        else:
            if r.ok:
                return r.json()
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise JevError(f"Błąd API {r.status_code} {HINTS.get(r.status_code, '')}\n{r.text[:500]}")
        time.sleep(2 ** attempt)
    raise JevError("Nieosiągalne")
