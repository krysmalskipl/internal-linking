import pytest

from internal_linking.jev import client


class Response:
    ok, status_code, text = True, 200, ""

    def json(self):
        return {"answers": {}}


@pytest.fixture
def sent(tmp_path, monkeypatch):
    """Records every request instead of sending it; no .env from the developer's directory."""
    monkeypatch.chdir(tmp_path)
    for name in ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "TYPESAFE_MODEL", "TYPESAFE_API_URL", "JEV_MODEL",
                 "JEV_API_URL"):
        monkeypatch.delenv(name, raising=False)
    calls = []
    monkeypatch.setattr(client.requests, "post", lambda url, json, headers, timeout: calls.append(
        (url, json["model"], headers["Authorization"])) or Response())
    return calls


def test_jev_uses_typesafe_when_its_key_is_set(sent, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
    client.decide({"a": 1}, {})
    assert sent == [("https://api.typesafe.ai/v1/systemone", "jev-1.13.0", "Bearer ts-key")]
    assert client.jev_provider() == "TypeSafe"


def test_jev_falls_back_to_openrouter_when_the_typesafe_key_is_empty(sent, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    client.decide({"a": 1}, {})
    assert sent == [("https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13", "Bearer sk-or")]
    assert client.jev_provider() == "OpenRouter"


def test_keys_are_read_from_dotenv(sent, tmp_path):
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or\nTYPESAFE_API_KEY=\n")
    client.decide({"a": 1}, {})
    assert sent[0][2] == "Bearer sk-or"


def test_no_key_for_jev_names_both_options(sent):
    with pytest.raises(client.JevError, match="TYPESAFE_API_KEY .* OPENROUTER_API_KEY"):
        client.decide({"a": 1}, {})
    assert not sent


def test_embeddings_keep_the_openrouter_key_with_a_typesafe_key_set(sent, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
    client.post("https://openrouter.ai/api/v1/embeddings", {"model": "qwen/qwen3-embedding-8b"})
    assert sent[0][2] == "Bearer sk-or"
