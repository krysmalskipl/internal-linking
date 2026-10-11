import http.client
import json
import socket
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from internal_linking.common import set_data_root, write_csv, write_jsonl
from internal_linking.ui import server

from conftest import page


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ui(tmp_path, monkeypatch):
    """The panel's server on a free port, over a data folder with one run domain."""
    monkeypatch.chdir(tmp_path)
    for k in server.KEYS:
        monkeypatch.delenv(k, raising=False)
    set_data_root(tmp_path / "data")
    d = tmp_path / "data" / "example.pl"
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"run_at": "2026-10-11T09:12+02:00", "links": 3, "pages": 5,
                                            "stats": {"Jev cost": "$0.004"}}), encoding="utf-8")
    (d / "report.html").write_text("<html>report</html>", encoding="utf-8")
    write_csv(d / "verify.csv", [{"status": "ok", "source_url": "https://example.pl/a/", "anchor": "x", "target_url": "https://example.pl/b/",
                                  "checked": "2026-10-11T10:00", "where": "live"},
                                 {"status": "missing", "source_url": "https://example.pl/a/", "anchor": "y", "target_url": "https://example.pl/c/",
                                  "checked": "2026-10-11T10:00", "where": "live"}],
              ["status", "source_url", "anchor", "target_url", "found_text", "note", "checked", "where"])
    write_csv(d / "recommendations.csv", [], ["decision", "source_url", "target_url", "anchor"])
    write_jsonl(d / "pages.jsonl", [page("https://example.pl/", "Start", "tekst")])
    (tmp_path / ".env").write_text("# my keys\nOTHER=keep-me\nOPENROUTER_API_KEY=sk-or-old-key-1234\n", encoding="utf-8")
    port, token = free_port(), "test-token"
    jobs = server.Jobs(None)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), server.make_handler(port, token, jobs, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def request(method, path, body=None, token_=token, host=f"127.0.0.1:{port}", raw=False):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        headers = {"Host": host, "X-Token": token_}
        data = body if raw else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        payload = r.read()
        try:
            return r.status, json.loads(payload)
        except ValueError:
            return r.status, payload.decode("utf-8", "replace")

    yield types_ns(request=request, dir=d, root=tmp_path, jobs=jobs)
    jobs.stop_all()
    httpd.shutdown()


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


def wait(ui, job_id, timeout=60):
    end, lines = time.time() + timeout, []
    while time.time() < end:
        status, s = ui.request("GET", f"/api/jobs/{job_id}?from={len(lines)}")
        lines += s["lines"]
        if s["status"] != "running":
            return s, lines
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_page_carries_the_token_and_foreign_hosts_and_missing_tokens_are_refused(ui):
    status, html = ui.request("GET", "/")
    assert status == 200 and 'data-token="test-token"' in html
    assert ui.request("GET", "/api/projects", host="evil.example:80")[0] == 403          # DNS rebinding
    assert ui.request("POST", "/api/jobs", {"command": "report", "domain": "example.pl"}, token_="")[0] == 403


def test_projects_list_runs_and_verification(ui):
    status, projects = ui.request("GET", "/api/projects")
    p = projects[0]
    assert status == 200 and p["domain"] == "example.pl" and p["links"] == 3 and p["stats"]["Jev cost"] == "$0.004"
    assert p["verified"]["ok"] == 1 and p["verified"]["total"] == 2
    assert {"report.html", "run.json"} <= {f["name"] for f in p["files"]}


def test_keys_are_saved_to_env_and_never_sent_back(ui, monkeypatch):
    status, s = ui.request("POST", "/api/settings", {"keys": {"TYPESAFE_API_KEY": "ts-new-key-9876", "OPENROUTER_API_KEY": "sk-or-new-5555"}})
    env = (ui.root / ".env").read_text(encoding="utf-8")
    assert status == 200 and "OTHER=keep-me" in env and "# my keys" in env
    assert "OPENROUTER_API_KEY=sk-or-new-5555" in env and "TYPESAFE_API_KEY=ts-new-key-9876" in env and "old-key" not in env
    assert "sk-or-new" not in json.dumps(s) and s["keys"]["OPENROUTER_API_KEY"] == {"set": True, "hint": "…5555", "from": ".env"}
    assert ui.request("POST", "/api/settings", {"keys": {"TYPESAFE_API_KEY": "has space"}})[0] == 400
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-the-shell-0000")
    assert ui.request("GET", "/api/settings")[1]["keys"]["TYPESAFE_API_KEY"]["from"] == "shell"


def test_thresholds_are_checked_and_only_changes_are_saved(ui):
    status, s = ui.request("POST", "/api/settings", {"config": {"score_min": "0.95", "archive_patterns": ["/tag/"]}})
    assert status == 200 and s["config"]["score_min"] == 0.95 and s["config_path"] == str(ui.root / "config.json")
    assert json.loads((ui.root / "config.json").read_text(encoding="utf-8")) == {"score_min": 0.95, "archive_patterns": ["/tag/"]}
    assert ui.request("POST", "/api/settings", {"config": {"score_min": 2}})[0] == 400
    assert ui.request("POST", "/api/settings", {"config": {"max_links_per_target": 0}})[1]["config"]["max_links_per_target"] == 0
    assert ui.request("POST", "/api/settings", {"config": {"max_links_per_target": 31}})[0] == 400
    assert ui.request("POST", "/api/settings", {"config": {"archive_patterns": ["(unclosed"]}})[0] == 400
    assert ui.request("POST", "/api/settings", {"config": {"rm -rf": 1}})[0] == 400


def test_a_job_runs_the_cli_with_its_output_live_and_in_a_log(ui):
    status, job = ui.request("POST", "/api/jobs", {"command": "report", "domain": "https://www.example.pl/", "options": {"max_links": 4}})
    assert status == 201 and job["status"] == "running" and job["domain"] == "example.pl"
    state, lines = wait(ui, job["id"])
    assert state["status"] == "done" and state["code"] == 0
    assert any("example.pl: 0 suggestions" in line for line in lines)
    log = ui.dir / state["log"]
    assert log.parent.name == "logs" and "report --domain example.pl --max-links 4" in log.read_text(encoding="utf-8")
    assert ui.request("GET", "/api/projects")[1][0]["logs"][0]["name"] == state["log"]


def test_one_job_at_a_time_per_domain_and_stop(ui):
    write_csv(ui.dir / "sample_to_label.csv", [], ["ok", "source_url", "target_url"])
    status, job = ui.request("POST", "/api/jobs", {"command": "label", "domain": "example.pl", "options": {"port": free_port()}})
    assert status == 201
    assert ui.request("POST", "/api/jobs", {"command": "report", "domain": "example.pl"})[0] == 409
    ui.request("POST", f"/api/jobs/{job['id']}/stop", {})
    state, _ = wait(ui, job["id"])
    assert state["status"] == "stopped"


def test_only_listed_options_with_valid_values_reach_the_cli(ui):
    bad = [{"command": "rm", "domain": "example.pl"}, {"command": "run", "domain": "example.pl; ls"},
           {"command": "run", "domain": "example.pl", "options": {"config": "/etc/passwd"}},
           {"command": "run", "domain": "example.pl", "options": {"max_links": "3; ls"}},
           {"command": "run", "domain": "example.pl", "options": {"sources": "(unclosed"}},
           {"command": "verify", "domain": "example.pl", "options": {"base_url": "file:///etc/passwd"}},
           {"command": "run", "domain": "example.pl", "options": {"keywords": "../../etc/passwd"}},
           {"command": "run", "domain": "example.pl", "options": {"keywords": "not-uploaded.csv"}}]
    for body in bad:
        assert ui.request("POST", "/api/jobs", body)[0] == 400, body
    args = server.command_args("run", "example.pl", {"no_crawl": True, "dry_run": False, "max_links": "5", "sources": "^/blog/"})
    assert args == ["run", "--domain", "example.pl", "--no-crawl", "--max-links", "5", "--sources", "^/blog/"]
    assert server.command_args("verify", "example.pl", {"links": "latest"})[-2:] == ["--links", "latest"]


def test_uploads_and_files_stay_inside_the_domain_folder(ui):
    status, f = ui.request("POST", "/api/upload/example.pl?name=keywords.csv", "keyword\nwymiana opon\n".encode(), raw=True)
    assert status == 201 and (ui.dir / "inputs" / "keywords.csv").read_text(encoding="utf-8").startswith("keyword")
    args = server.command_args("run", "example.pl", {"keywords": "keywords.csv"})
    assert args[-1] == str((ui.dir / "inputs" / "keywords.csv").resolve())
    assert ui.request("POST", "/api/upload/example.pl?name=../evil.csv", b"x", raw=True)[0] == 400
    assert ui.request("POST", "/api/upload/example.pl?name=run.sh", b"x", raw=True)[0] == 400
    assert ui.request("GET", "/files/example.pl/report.html") == (200, "<html>report</html>")
    for path in ("/files/example.pl/../../.env", "/files/example.pl/%2e%2e/%2e%2e/.env", "/files/example.pl/..%2f..%2f.env",
                 "/files/..%2f/.env", "/files/example.pl/nope.html"):
        assert ui.request("GET", path)[0] in (400, 404), path


def test_page_script_is_valid_javascript():
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    check = subprocess.run(["node", "--check", str(server.STATIC / "app.js")], capture_output=True, text=True, encoding="utf-8")
    assert check.returncode == 0, check.stderr


def test_a_command_that_fails_shows_as_failed_with_its_message(ui):
    status, job = ui.request("POST", "/api/jobs", {"command": "report", "domain": "nowa-domena.pl"})
    state, lines = wait(ui, job["id"])
    assert state["status"] == "failed" and state["code"] == 1
    assert any("run `internal-linking run --domain nowa-domena.pl` first" in line for line in lines)
