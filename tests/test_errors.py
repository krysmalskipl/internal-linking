"""A stranger's first runs: every mistake ends with a message and an exit code, never a trace,
and a key or credit problem stops before money is spent on calls that would all fail."""
import json
import threading
import time
import types

import pytest
import requests

from internal_linking import cli, crawl as crawl_mod, pipeline
from internal_linking.common import read_jsonl
from internal_linking.jev import client, pairs

ANSWER = {"answers": {"kontekst": {"noul": 0.9}, "anchor": {"noul": 0.9}, "wartosc": {"score": 3.0},
                      "kanibalizacja": {"noul": 0.1}, "powod": {"choice": "ok", "probabilities": {"ok": 0.97}}}}


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def run_cli(*argv):
    with pytest.raises(SystemExit) as stop:
        cli.main(["--data-dir", "data", *argv])
    return stop.value.code


@pytest.mark.parametrize("argv, message", [
    (["sample", "--domain", "example.pl"], "recommendations.csv - run `internal-linking run --domain example.pl` first"),
    (["evaluate", "--domain", "example.pl"], "sample_to_label.csv - draw a sample first"),
    (["label", "--domain", "example.pl"], "sample_to_label.csv - draw a sample first"),
    (["report", "--domain", "example.pl"], "run `internal-linking run --domain example.pl` first"),
    (["embed", "--domain", "example.pl"], "no pages.jsonl"),
    (["run", "--domain", "example.pl", "--sources", "(bad"], "--sources: not a valid regular expression"),
    (["run", "--domains-file", "missing.txt"], "no such file: missing.txt"),
])
def test_mistakes_end_with_a_message_and_exit_code_1(cwd, capsys, argv, message):
    assert run_cli(*argv) == 1
    out = capsys.readouterr()
    assert message in out.out + out.err and "Traceback" not in out.out + out.err


def test_a_broken_config_json_is_named(cwd, capsys):
    (cwd / "config.json").write_text("{score_min: 0.9", encoding="utf-8")
    assert run_cli("report", "--domain", "example.pl") == 1
    assert "config.json is not valid JSON" in capsys.readouterr().err


def test_an_unexpected_error_asks_for_a_report_and_debug_shows_the_trace(cwd, capsys, monkeypatch):
    def boom(args):
        raise KeyError("x")
    monkeypatch.setattr(pipeline, "add_parser", lambda sub: sub.add_parser("run").set_defaults(func=boom))
    assert run_cli("run") == 1
    assert "unexpected error (KeyError" in capsys.readouterr().err
    with pytest.raises(KeyError):
        cli.main(["--debug", "run"])


def test_ctrl_c_exits_130(cwd, capsys, monkeypatch):
    def interrupted(args):
        raise KeyboardInterrupt
    monkeypatch.setattr(pipeline, "add_parser", lambda sub: sub.add_parser("run").set_defaults(func=interrupted))
    assert run_cli("run") == 130
    assert "stopped (Ctrl+C)" in capsys.readouterr().err


def test_a_failed_domain_fails_the_run(cwd, monkeypatch, capsys):
    def no_site(domain, sitemap, delay):
        raise RuntimeError("cannot reach https://example.pl: the domain does not resolve (DNS)")
    monkeypatch.setattr(pipeline, "crawl", no_site)
    assert run_cli("run", "--domain", "example.pl", "--no-embeddings") == 1
    assert "example.pl: cannot reach" in capsys.readouterr().out


def rows(n):
    return [{"_state": {"pair": i}, "lang": "pl"} for i in range(n)]


def test_a_bad_key_stops_the_run_instead_of_paying_for_every_pair(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    calls, lock = [], threading.Lock()

    def judge(state, lang):
        with lock:
            calls.append(state)
        time.sleep(0.05)
        raise client.JevError("API error 402 (no credits left on the account)", 402)
    monkeypatch.setattr(pairs, "judge", judge)
    with pytest.raises(client.FatalApiError, match="stopped before sending the remaining pairs"):
        pipeline.judge(rows(60), tmp_path, workers=3)
    time.sleep(0.2)
    assert len(calls) <= 6  # the calls already running, not the 60 queued


def test_a_bad_key_stops_all_domains(cwd, monkeypatch, capsys):
    seen = []

    def process(d, args, cfg):
        seen.append(d)
        raise client.FatalApiError("Jev: API error 401 (missing or invalid key)")
    monkeypatch.setattr(pipeline, "process", process)
    assert run_cli("run", "--domain", "a.pl", "--domain", "b.pl") == 1
    assert seen == ["a.pl"] and "401" in capsys.readouterr().err


def test_a_malformed_answer_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    answers = iter([ANSWER, {"error": "oops"}, ANSWER, ANSWER, ANSWER])
    monkeypatch.setattr(pairs, "judge", lambda state, lang: next(answers))
    batch = rows(5)
    pipeline.judge(batch, tmp_path, workers=1)
    cached = read_jsonl(tmp_path / "jev_pairs.jsonl")
    assert len(cached) == 4 and all(pairs.well_formed(c["response"]) for c in cached)
    assert sum("score" in r for r in batch) == 4
    # an answer an older version cached in a bad shape is asked again, not crashed on
    (tmp_path / "jev_pairs.jsonl").write_text(json.dumps({"key": pairs.cache_key({"pair": 9}, "pl", client.jev_version()),
                                                          "response": {"error": "old"}}) + "\n",
                                              encoding="utf-8")
    monkeypatch.setattr(pairs, "judge", lambda state, lang: ANSWER)
    again = rows(10)[9:]
    pipeline.judge(again, tmp_path, workers=1)
    assert again[0]["score"] == 0.97


def test_rate_limits_wait_as_long_as_the_api_asks(monkeypatch):
    replies = iter([types.SimpleNamespace(ok=False, status_code=429, headers={"Retry-After": "7"}, text="slow down"),
                    types.SimpleNamespace(ok=True, status_code=200, headers={}, json=lambda: {"fine": True})])
    waits = []
    monkeypatch.setattr(client.requests, "post", lambda *a, **kw: next(replies))
    monkeypatch.setattr(client.time, "sleep", waits.append)
    assert client.post("https://api.example/x", {}, key="k") == {"fine": True}
    assert waits == [7.0]


def test_api_errors_are_short_and_carry_their_status(monkeypatch):
    monkeypatch.setattr(client.requests, "post", lambda *a, **kw: types.SimpleNamespace(
        ok=False, status_code=401, headers={}, text='{"error": "bad key"}'))
    with pytest.raises(client.JevError) as e:
        client.post("https://api.example/x", {}, key="k")
    assert e.value.status == 401 and e.value.fatal and "missing or invalid key" in str(e.value)

    def offline(*a, **kw):
        raise requests.ConnectionError("HTTPSConnectionPool(...): Max retries exceeded (Caused by NameResolutionError(...))")
    monkeypatch.setattr(client.requests, "post", offline)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    with pytest.raises(client.JevError, match=r"the domain does not resolve \(DNS\)$"):
        client.post("https://api.example/x", {}, key="k")


def fake_site(pages):
    def fetch(url):
        body, kind = pages.get(url, (None, ""))
        if body is None:
            return types.SimpleNamespace(ok=False, status_code=404, text="", content=b"", url=url, headers={})
        return types.SimpleNamespace(ok=True, status_code=200, text=body, content=body.encode(), url=url,
                                     headers={"content-type": kind})
    return fetch


SITEMAP = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<url><loc>https://example.pl/a/</loc></url><url><loc>https://example.pl/cennik.pdf</loc></url></urlset>')


def test_when_no_page_can_be_used_the_crawl_says_why(tmp_path, monkeypatch):
    from internal_linking.common import set_data_root
    set_data_root(tmp_path)
    monkeypatch.setattr(crawl_mod, "reachable", lambda base: None)
    monkeypatch.setattr(crawl_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(crawl_mod, "fetch", fake_site({
        "https://example.pl/robots.txt": ("User-agent: *\nDisallow: /\n", "text/plain"),
        "https://example.pl/sitemap.xml": (SITEMAP, "application/xml")}))
    with pytest.raises(RuntimeError, match=r"none of the 2 pages .* disallowed by robots.txt \(2\)"):
        crawl_mod.crawl("example.pl")


def test_files_that_are_not_web_pages_are_skipped(tmp_path, monkeypatch, capsys):
    from internal_linking.common import set_data_root
    set_data_root(tmp_path)
    html = "<html><body><main><p>" + "Treść strony o remontach. " * 20 + "</p></main></body></html>"
    monkeypatch.setattr(crawl_mod, "reachable", lambda base: None)
    monkeypatch.setattr(crawl_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(crawl_mod, "fetch", fake_site({
        "https://example.pl/robots.txt": ("", "text/plain"),
        "https://example.pl/sitemap.xml": (SITEMAP, "application/xml"),
        "https://example.pl/a/": (html, "text/html; charset=utf-8"),
        "https://example.pl/cennik.pdf": ("%PDF-1.7", "application/pdf")}))
    pages = crawl_mod.crawl("example.pl")
    assert [p["url"] for p in pages] == ["https://example.pl/a/"]
    assert "cennik.pdf: not a web page (application/pdf)" in capsys.readouterr().out


def test_an_unreachable_site_is_one_plain_sentence(monkeypatch):
    with pytest.raises(RuntimeError, match=r"^cannot reach https://example\.pl: .* - check the domain"):
        crawl_mod.reachable("https://example.pl")  # the test network guard refuses it like a dead DNS would
