"""API client: Jev decisions (TypeSafe's own API or OpenRouter) and, through post(), the embeddings API.

Jev goes to TypeSafe directly when TYPESAFE_API_KEY is set, otherwise through OpenRouter with
OPENROUTER_API_KEY; embeddings always use OpenRouter. Raises exceptions instead of exiting and
retries on 429 / 5xx. Keys come from the environment or a .env file in the working directory.
"""
import os
import re
import time
from pathlib import Path

import requests

API_URL_DEFAULT = "https://openrouter.ai/api/alpha/decisions"
MODEL_DEFAULT = "typesafe/jev-1.13"
TYPESAFE_URL_DEFAULT = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL_DEFAULT = "jev-1.13.0"  # the same Jev version as typesafe/jev-1.13 on OpenRouter
RETRY_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}  # 52x: Cloudflare edge errors
HINTS = {401: "missing or invalid key", 402: "no credits left on the account", 403: "the key may not use this model",
         404: "model or endpoint not found - check JEV_MODEL / EMBEDDING_MODEL and the API URL in .env",
         429: "rate limited"}
FATAL_STATUSES = {401, 402, 403, 404}  # every further call would fail the same way
MAX_WAIT = 60                         # longest Retry-After honoured, in seconds


class JevError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def fatal(self) -> bool:
        """A key, credit or model problem: stop instead of paying for calls that will all fail."""
        return self.status in FATAL_STATUSES


class FatalApiError(JevError):
    """A key, credit or model problem: ends the whole run, not just the domain being processed."""


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


def env(name: str) -> str:
    load_dotenv()
    return os.environ.get(name, "").strip()


def api_key() -> str:
    """The OpenRouter key (embeddings, and Jev when there is no TypeSafe key)."""
    key = env("OPENROUTER_API_KEY")
    if not key:
        raise JevError("OPENROUTER_API_KEY is not set. Put it in .env in the working directory "
                       "(OPENROUTER_API_KEY=sk-or-...) or export it.")
    return key


def jev_provider() -> str:
    return "TypeSafe" if env("TYPESAFE_API_KEY") else "OpenRouter"


def jev_version() -> str:
    """The Jev version the calls go to, named the same for both providers (typesafe/jev-1.13 on
    OpenRouter and jev-1.13.0 on TypeSafe are both jev-1.13). Part of the answer cache key."""
    if env("TYPESAFE_API_KEY"):
        model = env("TYPESAFE_MODEL") or TYPESAFE_MODEL_DEFAULT
    else:
        model = env("JEV_MODEL") or MODEL_DEFAULT
    return re.sub(r"(\.0)+$", "", model.removeprefix("typesafe/"))


def jev_endpoint() -> tuple[str, str, str]:
    """(url, model, key) for Jev: TypeSafe's own API when TYPESAFE_API_KEY is set (an empty value
    counts as not set), otherwise OpenRouter."""
    if key := env("TYPESAFE_API_KEY"):
        return env("TYPESAFE_API_URL") or TYPESAFE_URL_DEFAULT, env("TYPESAFE_MODEL") or TYPESAFE_MODEL_DEFAULT, key
    if not env("OPENROUTER_API_KEY"):
        raise JevError("No key for Jev. Put TYPESAFE_API_KEY (from console.typesafe.ai) or OPENROUTER_API_KEY "
                       "in .env in the working directory, or export it.")
    return env("JEV_API_URL") or API_URL_DEFAULT, env("JEV_MODEL") or MODEL_DEFAULT, api_key()


def post(url: str, body: dict, key: str | None = None, retries: int = 4, timeout: int = 60) -> dict:
    """POST with a bearer key (the OpenRouter key by default); retries on 429, 5xx and network errors."""
    headers = {"Authorization": f"Bearer {key or api_key()}", "Content-Type": "application/json",
               "X-Title": "internal-linking"}
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=body, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            if attempt == retries:
                raise JevError(f"Cannot reach the API ({url}): {short_error(e)}") from e
            wait = 2 ** attempt
        else:
            if r.ok:
                try:
                    return r.json()
                except ValueError:
                    raise JevError(f"The API answered with something other than JSON: {r.text[:200]}") from None
            if r.status_code not in RETRY_STATUSES or attempt == retries:
                raise JevError(f"API error {r.status_code} ({HINTS.get(r.status_code, 'see the details')})\n{r.text[:500]}",
                               r.status_code)
            wait = retry_after(r) or 2 ** attempt
        time.sleep(wait)
    raise JevError("unreachable")


def retry_after(r: requests.Response) -> float | None:
    """Seconds the API asks to wait before the next call (Retry-After on 429 / 503), at most MAX_WAIT."""
    try:
        return min(max(float(r.headers.get("Retry-After", "")), 0), MAX_WAIT)
    except ValueError:
        return None


def short_error(e: Exception) -> str:
    """A network error in a few words instead of urllib3's nested message."""
    text = str(e)
    for needle, plain in (("NameResolution", "the domain does not resolve (DNS)"),
                          ("Failed to resolve", "the domain does not resolve (DNS)"),
                          ("getaddrinfo", "the domain does not resolve (DNS)"),
                          ("CERTIFICATE_VERIFY_FAILED", "invalid TLS certificate"),
                          ("Connection refused", "connection refused"),
                          ("timed out", "timed out"), ("Read timed out", "timed out")):
        if needle in text:
            return plain
    if isinstance(e, requests.exceptions.SSLError):
        return "TLS/SSL error"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "no connection"
    return type(e).__name__


def decide(state: dict, questions: dict, retries: int = 4, timeout: int = 60) -> dict:
    url, model, key = jev_endpoint()
    return post(url, {"model": model, "state": state, "questions": questions}, key, retries, timeout)
