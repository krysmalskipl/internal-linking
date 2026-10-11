"""`internal-linking ui`: the tool in the browser, on your own computer.

A small local web app over the same files and commands as the CLI: the domains in data/, their last
run, report, verification and files; forms that start run, embed, report, verify, sample, evaluate
and label with their output live on the page; the keys in .env and the thresholds in config.json.

Every action is the CLI itself, started as a subprocess with options from a fixed list, so it
behaves exactly as in a terminal and can be stopped. Its output is kept in
data/<domain>/logs/<time>-<command>.log.

The server listens on 127.0.0.1 only, answers only requests addressed to it (Host header - no DNS
rebinding), accepts a change only with the token written into its own page (no other site can
trigger one), never sends a key back - only whether it is set and its last characters - and serves
files from data/<domain>/ only.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__, verify
from ..common import data_root, now
from ..config import DEFAULT_PATH, load_config, save_config

STATIC = Path(__file__).parent
KEYS = ["OPENROUTER_API_KEY", "TYPESAFE_API_KEY"]
DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
FILE_NAME = re.compile(r"^[\w][\w.-]*$")
UPLOAD_TYPES = {".csv", ".txt", ".md"}
UPLOAD_MAX = 20 * 1024 * 1024
TYPES = {".html": "text/html", ".csv": "text/csv", ".md": "text/markdown", ".json": "application/json",
         ".jsonl": "application/x-ndjson", ".log": "text/plain", ".txt": "text/plain", ".js": "text/javascript",
         ".css": "text/css"}

# the options each command takes from the page: name → (flag, kind); kinds are checked before use
OPTIONS = {
    "run": {"no_crawl": ("--no-crawl", "flag"), "no_embeddings": ("--no-embeddings", "flag"),
            "dry_run": ("--dry-run", "flag"), "max_links": ("--max-links", "int"), "sources": ("--sources", "regex"),
            "targets": ("--targets", "regex"), "sources_file": ("--sources-file", "input"),
            "targets_file": ("--targets-file", "input"), "keywords": ("--keywords", "input"),
            "sitemap": ("--sitemap", "url"), "delay": ("--delay", "float"), "workers": ("--workers", "int")},
    "embed": {"workers": ("--workers", "int")},
    "report": {"max_links": ("--max-links", "int")},
    "verify": {"base_url": ("--base-url", "url"), "links": ("--links", "links"), "delay": ("--delay", "float")},
    "sample": {"n": ("--n", "int")},
    "evaluate": {"target_precision": ("--target-precision", "float"), "write_config": ("--write-config", "flag")},
    "label": {"port": ("--port", "int")},
}
# config.json fields the settings page edits: name → (kind, min, max)
CONFIG_FIELDS = {"score_min": ("float", 0, 1), "context_min": ("float", 0, 1), "anchor_min": ("float", 0, 1),
                 "cannibalisation_max": ("float", 0, 1), "similarity_min": ("float", 0, 1),
                 "template_share": ("float", 0, 1), "max_links_per_page": ("int", 1, 50),
                 "min_words": ("int", 0, 5000), "archive_patterns": ("regexes", 0, 0)}


class Problem(Exception):
    """A request the server refuses, with the reason shown on the page."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def clean_domain(value: str) -> str:
    d = re.sub(r"^https?://", "", str(value or "").strip().lower()).strip("/")
    d = d.removeprefix("www.")
    if not DOMAIN.match(d):
        raise Problem(f"not a domain: {value}")
    return d


def domain_dir(domain: str) -> Path:
    return data_root() / clean_domain(domain)


def inside(base: Path, rel: str) -> Path:
    """base/rel, refusing anything that would leave base (../, absolute paths, links)."""
    path = (base / unquote(rel)).resolve()
    if not path.is_relative_to(base.resolve()):
        raise Problem("no such file", 404)
    return path


# ---- .env and config.json -------------------------------------------------------------------

def env_file() -> Path:
    return Path.cwd() / ".env"


def read_env() -> dict[str, str]:
    values = {}
    if env_file().exists():
        for line in env_file().read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip("\"'")
    return values


def key_status() -> dict:
    """Whether each key is set and where - never the key itself, only its last 4 characters."""
    in_file, out = read_env(), {}
    for k in KEYS:
        shell, file = os.environ.get(k, ""), in_file.get(k, "")
        value = shell or file
        out[k] = {"set": bool(value), "hint": "…" + value[-4:] if len(value) > 8 else ("set" if value else ""),
                  "from": "shell" if shell else ".env" if file else ""}
    return out


def write_keys(new: dict) -> None:
    """Sets (or with "" clears) keys in .env, keeping every other line as it was."""
    new = {k: str(v).strip() for k, v in new.items() if k in KEYS}
    if any(re.search(r"[\s\"'#]", v) for v in new.values()):
        raise Problem("a key cannot contain spaces, quotes or #")
    lines = env_file().read_text(encoding="utf-8").splitlines() if env_file().exists() else []
    out, done = [], set()
    for line in lines:
        name = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
        if name in new:
            out.append(f"{name}={new[name]}")
            done.add(name)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in new.items() if k not in done]
    env_file().write_text("\n".join(out) + "\n", encoding="utf-8")


def settings(config: str | None) -> dict:
    return {"keys": key_status(), "config": load_config(config), "defaults": json.loads(DEFAULT_PATH.read_text()),
            "config_path": str(saved_to(config)), "env_path": str(env_file()), "data_dir": str(data_root().resolve()),
            "version": __version__}


def saved_to(config: str | None) -> Path:
    """Where the settings page writes thresholds: --config or ./config.json, never the packaged defaults."""
    return (Path(config) if config else Path.cwd() / "config.json").resolve()


def write_config(values: dict, config: str | None) -> None:
    """Checks the values and saves only what differs from the built-in defaults."""
    defaults, cfg = json.loads(DEFAULT_PATH.read_text()), load_config(config)
    for name, value in values.items():
        if name not in CONFIG_FIELDS:
            raise Problem(f"unknown setting: {name}")
        kind, lo, hi = CONFIG_FIELDS[name]
        if kind == "regexes":
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise Problem("archive_patterns: a list of regexes")
            for x in value:
                try:
                    re.compile(x)
                except re.error as e:
                    raise Problem(f"archive_patterns: {x} - {e}")
        else:
            try:
                value = int(value) if kind == "int" else float(value)
            except (TypeError, ValueError):
                raise Problem(f"{name}: not a number")
            if not lo <= value <= hi:
                raise Problem(f"{name}: between {lo} and {hi}")
        cfg[name] = value
    save_config({k: v for k, v in cfg.items() if defaults.get(k) != v}, config)


# ---- projects and files ---------------------------------------------------------------------

def file_info(path: Path, base: Path) -> dict:
    st = path.stat()
    return {"name": str(path.relative_to(base)), "size": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="minutes")}


def project(d: Path, jobs: "Jobs") -> dict:
    run = json.loads((d / "run.json").read_text()) if (d / "run.json").exists() else {}
    checked = verify.load(d)
    logs = sorted((d / "logs").glob("*.log"), reverse=True) if (d / "logs").is_dir() else []
    return {"domain": d.name, "run_at": run.get("run_at", ""), "links": run.get("links"), "pages": run.get("pages"),
            "stats": run.get("stats", {}),
            "verified": {"ok": sum(r["status"] == "ok" for r in checked), "total": len(checked),
                         "checked": checked[0].get("checked", "") if checked else "", "where": checked[0].get("where", "") if checked else "",
                         "rows": checked},
            "files": [file_info(f, d) for f in sorted(d.iterdir()) if f.is_file()],
            "inputs": [file_info(f, d) for f in sorted((d / "inputs").iterdir()) if f.is_file()] if (d / "inputs").is_dir() else [],
            "logs": [file_info(f, d) for f in logs[:30]],
            "job": jobs.running(d.name)}


def projects(jobs: "Jobs") -> list[dict]:
    root = data_root()
    if not root.is_dir():
        return []
    found = [project(d, jobs) for d in root.iterdir() if d.is_dir() and DOMAIN.match(d.name)]
    return sorted(found, key=lambda p: p["run_at"] or "", reverse=True)


# ---- jobs: the CLI as a subprocess ------------------------------------------------------------

def command_args(command: str, domain: str, options: dict) -> list[str]:
    """CLI arguments for a job, from the options allowed for its command only."""
    if command not in OPTIONS:
        raise Problem(f"unknown command: {command}")
    args = [command, "--domain", domain]
    for name, value in (options or {}).items():
        if name not in OPTIONS[command]:
            raise Problem(f"{command} has no option {name}")
        flag, kind = OPTIONS[command][name]
        if value in (None, "", False):
            continue
        if kind == "flag":
            args.append(flag)
            continue
        value = str(value).strip()
        if kind == "int" and not re.fullmatch(r"\d{1,6}", value):
            raise Problem(f"{name}: a whole number")
        if kind == "float" and not re.fullmatch(r"\d{1,4}(\.\d+)?", value):
            raise Problem(f"{name}: a number")
        if kind == "url" and not re.fullmatch(r"https?://[^\s\"'<>]+", value):
            raise Problem(f"{name}: an http(s) address")
        if kind == "regex":
            try:
                re.compile(value)
            except re.error as e:
                raise Problem(f"{name}: {e}")
        if kind in ("input", "links"):
            if kind == "links" and value == "latest":
                args += [flag, value]
                continue
            if not FILE_NAME.match(value):
                raise Problem(f"{name}: pick an uploaded file")
            path = domain_dir(domain) / "inputs" / value
            if not path.is_file():
                raise Problem(f"{name}: {value} is not uploaded")
            value = str(path.resolve())
        args += [flag, value]
    return args


class Job:
    def __init__(self, job_id: str, domain: str, command: str, argv: list[str], log: Path):
        self.id, self.domain, self.command, self.argv, self.log = job_id, domain, command, argv, log
        self.lines: list[str] = []
        self.status, self.code, self.started, self.ended, self.stopped = "running", None, now(), "", False
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", env=env)
        threading.Thread(target=self.pump, daemon=True).start()

    def pump(self) -> None:
        with self.log.open("w", encoding="utf-8") as out:
            out.write(f"$ internal-linking {' '.join(self.argv[3:])}\n")
            for line in self.proc.stdout:
                self.lines.append(line.rstrip("\n"))
                out.write(line)
                out.flush()
            self.code = self.proc.wait()
            self.ended = now()
            self.status = "stopped" if self.stopped else "done" if self.code == 0 else "failed"
            out.write(f"\n[{self.status}, exit code {self.code}, {self.ended}]\n")

    def stop(self) -> None:
        if self.status == "running":
            self.stopped = True
            self.proc.terminate()

    def state(self, since: int = 0) -> dict:
        return {"id": self.id, "domain": self.domain, "command": self.command, "status": self.status, "code": self.code,
                "started": self.started, "ended": self.ended, "log": str(self.log.relative_to(self.log.parents[1])),
                "lines": self.lines[since:], "next": len(self.lines)}


class Jobs:
    def __init__(self, config: str | None):
        self.config, self.all, self.lock = config, {}, threading.Lock()

    def running(self, domain: str) -> str:
        return next((j.id for j in self.all.values() if j.domain == domain and j.status == "running"), "")

    def start(self, command: str, domain: str, options: dict) -> Job:
        domain = clean_domain(domain)
        args = command_args(command, domain, options)
        with self.lock:
            if self.running(domain):
                raise Problem(f"{domain}: another job is still running - wait for it or stop it", 409)
            logs = domain_dir(domain) / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            argv = [sys.executable, "-m", "internal_linking", "--data-dir", str(data_root().resolve())]
            argv += (["--config", self.config] if self.config else []) + args
            job = Job(secrets.token_hex(6), domain, command, argv, logs / f"{stamp}-{command}.log")
            self.all[job.id] = job
            return job

    def get(self, job_id: str) -> Job:
        if job_id not in self.all:
            raise Problem("no such job", 404)
        return self.all[job_id]

    def stop_all(self) -> None:
        for j in self.all.values():
            j.stop()


# ---- HTTP ------------------------------------------------------------------------------------

def make_handler(port: int, token: str, jobs: Jobs, config: str | None):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/j")) else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def json(self, data, status: int = 200) -> None:
            self.send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

        def handle_safely(self, method: str) -> None:
            try:
                if self.headers.get("Host") not in hosts:
                    raise Problem("wrong host", 403)
                url = urlsplit(self.path)
                if method == "POST" and not secrets.compare_digest(self.headers.get("X-Token", ""), token):
                    raise Problem("missing or wrong token - reload the page", 403)
                self.route(method, url.path, parse_qs(url.query))
            except Problem as e:
                self.json({"error": str(e)}, e.status)
            except Exception as e:  # noqa: BLE001 - shown on the page instead of a dropped connection
                self.json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_GET(self):
            self.handle_safely("GET")

        def do_POST(self):
            self.handle_safely("POST")

        def body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > UPLOAD_MAX:
                raise Problem("file too large (max 20 MB)", 413)
            return self.rfile.read(n)

        def route(self, method: str, path: str, query: dict) -> None:
            parts = [p for p in path.split("/") if p]
            if method == "GET" and path == "/":
                page = (STATIC / "index.html").read_text(encoding="utf-8")
                self.send(200, page.replace("__TOKEN__", token).replace("__VERSION__", __version__).encode(), "text/html")
            elif method == "GET" and len(parts) == 1 and parts[0] in ("app.js", "app.css"):
                self.send(200, (STATIC / parts[0]).read_bytes(), TYPES[Path(parts[0]).suffix])
            elif method == "GET" and path == "/api/projects":
                self.json(projects(jobs))
            elif method == "GET" and path == "/api/settings":
                self.json(settings(config))
            elif method == "POST" and path == "/api/settings":
                data = json.loads(self.body() or b"{}")
                if "keys" in data:
                    write_keys(data["keys"])
                if "config" in data:
                    write_config(data["config"], config)
                self.json(settings(config))
            elif method == "POST" and path == "/api/jobs":
                data = json.loads(self.body() or b"{}")
                self.json(jobs.start(data.get("command", ""), data.get("domain", ""), data.get("options", {})).state(), 201)
            elif len(parts) == 3 and parts[:2] == ["api", "jobs"] and method == "GET":
                self.json(jobs.get(parts[2]).state(int(query.get("from", ["0"])[0] or 0)))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "stop" and method == "POST":
                job = jobs.get(parts[2])
                job.stop()
                self.json(job.state(len(job.lines)))
            elif method == "POST" and len(parts) == 3 and parts[:2] == ["api", "upload"]:
                name = query.get("name", [""])[0]
                if not FILE_NAME.match(name) or Path(name).suffix.lower() not in UPLOAD_TYPES:
                    raise Problem("upload a .csv, .txt or .md file with a plain name")
                folder = domain_dir(parts[2]) / "inputs"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / name).write_bytes(self.body())
                self.json(file_info(folder / name, folder.parent), 201)
            elif method == "GET" and len(parts) >= 3 and parts[0] == "files":
                base = domain_dir(parts[1])
                file = inside(base, "/".join(parts[2:]))
                if not file.is_file():
                    raise Problem("no such file", 404)
                download = {"Content-Disposition": f'attachment; filename="{file.name}"'} if "download" in query else {}
                self.send(200, file.read_bytes(), TYPES.get(file.suffix.lower(), "application/octet-stream"), download)
            else:
                raise Problem("not found", 404)

        def log_message(self, *args):
            pass

    return Handler


def serve(port: int = 8770, open_browser: bool = True, config: str | None = None) -> None:
    token, jobs = secrets.token_urlsafe(24), Jobs(config)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(port, token, jobs, config))
    except OSError as e:
        sys.exit(f"! cannot listen on 127.0.0.1:{port} ({e.strerror}) - is the panel already open? "
                 f"Otherwise pick another port: internal-linking ui --port {port + 1}")
    url = f"http://127.0.0.1:{port}/"
    print(f"internal-linking {__version__} - open {url}  (data: {data_root().resolve()}; Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        jobs.stop_all()
        server.server_close()


def add_parser(sub) -> None:
    p = sub.add_parser("ui", help="the tool in the browser: run, verify, settings, files (localhost only)")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    p.set_defaults(func=lambda args: serve(args.port, not args.no_browser, args.config))
