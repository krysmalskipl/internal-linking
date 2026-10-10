"""Shared helpers: data directories, URL normalisation, page titles, JSONL and CSV I/O, progress messages."""
import csv
import json
import re
import time
from collections import Counter
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


def site_suffixes(pages: list[dict]) -> set[str]:
    """Title endings repeated across many pages (e.g. ' - Site name')."""
    c = Counter()
    for p in pages:
        m = re.search(r"\s[-–|]\s[^-–|]+$", p["title"])
        if m:
            c[m.group(0)] += 1
    return {s for s, n in c.items() if n >= max(3, 0.1 * len(pages))}


def clean_title(title: str, suffixes: set[str]) -> str:
    for s in suffixes:
        if title.endswith(s):
            return title[: -len(s)].strip()
    return title


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


class Progress:
    """Prints "label: done/total unit" every 10%, at least every 5 seconds and at the end, so long
    steps never look stuck."""

    def __init__(self, label: str, total: int, unit: str):
        self.label, self.total, self.unit, self.done = label, total, unit, 0
        self.every = max(1, total // 10)
        self.last = time.monotonic()

    def step(self, n: int = 1) -> None:
        before, self.done = self.done, self.done + n
        now = time.monotonic()
        if self.done // self.every > before // self.every or self.done == self.total or now - self.last >= 5:
            print(f"  {self.label}: {self.done}/{self.total} {self.unit}")
            self.last = now
