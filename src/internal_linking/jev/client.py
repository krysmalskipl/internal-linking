"""Jev (TypeSafe) client for the OpenRouter decisions API.

Raises exceptions instead of exiting and retries on 429 / 5xx. The API key comes from the
OPENROUTER_API_KEY environment variable or a .env file in the working directory.
"""
import os
import time
from pathlib import Path

import requests

API_URL_DEFAULT = "https://openrouter.ai/api/alpha/decisions"
MODEL_DEFAULT = "typesafe/jev-1.13"
RETRY_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}  # 52x: Cloudflare edge errors
HINTS = {401: "missing or invalid key", 402: "no credits left on the OpenRouter account",
         429: "rate limited"}


class JevError(RuntimeError):
    pass


def load_dotenv(path: Path | None = None) -> None:
    """Reads KEY=value lines from .env; variables already set in the shell take precedence."""
    path = path or Path.cwd() / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def api_key() -> str:
    load_dotenv()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise JevError("OPENROUTER_API_KEY is not set. Put it in .env in the working directory "
                       "(OPENROUTER_API_KEY=sk-or-...) or export it.")
    return key


def decide(state: dict, questions: dict, retries: int = 4, timeout: int = 60) -> dict:
    body = {"model": os.environ.get("JEV_MODEL", MODEL_DEFAULT), "state": state, "questions": questions}
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
               "X-Title": "internal-linking"}
    url = os.environ.get("JEV_API_URL", API_URL_DEFAULT)
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=body, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            if attempt == retries:
                raise JevError(f"Cannot reach the API: {e}") from e
        else:
            if r.ok:
                return r.json()
            if r.status_code not in RETRY_STATUSES or attempt == retries:
                raise JevError(f"API error {r.status_code} {HINTS.get(r.status_code, '')}\n{r.text[:500]}")
        time.sleep(2 ** attempt)
    raise JevError("unreachable")
