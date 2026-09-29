"""Shared helpers: data directories, URL normalisation, JSONL and CSV I/O."""
import csv
import json
from pathlib import Path
from urllib.parse import urlsplit

_data_root = Path("data")


def set_data_root(path: str | Path) -> None:
    global _data_root
    _data_root = Path(path)


def bare_host(host: str) -> str:
    host = host.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def data_dir(domain: str) -> Path:
    d = _data_root / bare_host(domain.removeprefix("https://").removeprefix("http://").strip("/"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def norm_url(url: str) -> str:
    """Comparison key: no www, query or fragment, consistent trailing slash."""
    p = urlsplit(url)
    path = p.path or "/"
    if not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"
    return f"{bare_host(p.netloc)}{path}"


def is_internal(url: str, domain: str) -> bool:
    return bare_host(urlsplit(url).netloc) == bare_host(domain)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    # utf-8-sig so Excel and Numbers show non-ASCII characters correctly
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
