"""After the links are inserted - by hand or by a coding agent - check that each one is really on the
page: in the content, with the expected link text, in the passage it was proposed for, pointing to a
page that loads. Works on the live site or on a local preview (--base-url, e.g. `astro dev`), so it
can run before a deploy and in CI (exit code 1 when anything is not ok).

The links come from links-for-agent.md (its CSV block) or a CSV with source_url, anchor and
target_url (links.csv, the report's CSV export). Only the pages that should get links are fetched.
Results go to data/<domain>/verify.csv and the report's Verification tab. Every `run` that crawls
again also checks the list it handed out last time, against the fresh pages, with no extra requests.
"""
import re
import sys
import time
import types
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import agent
from .common import data_dir, norm_url, read_csv, write_csv
from .crawl import ROBOTS_AGENT, extract, fetch, load_robots

FIELDS = ["status", "source_url", "anchor", "target_url", "found_text", "note", "checked", "where"]
PROBLEMS = ["missing", "other_text", "other_place", "outside_content", "target_error", "page_error"]
LABELS = {"ok": "inserted as proposed", "other_text": "linked, but with another link text",
          "other_place": "the link text is linked, but in another passage",
          "outside_content": "linked only from the menu, footer or sidebar", "missing": "not linked yet",
          "page_error": "the page did not load", "target_error": "the target page does not load"}
WINDOW = 4  # words on each side of the link text that place it in its passage


def words(text: str) -> list[str]:
    return re.findall(r"\w+", str(text).lower())


def in_passage(block_text: str, anchor: str, context: str) -> bool:
    """The block is the proposed passage: it holds the link text with the words around it there."""
    ctx, a = words(context), words(anchor)
    if not ctx or not a:
        return True  # nothing to compare with
    starts = [i for i in range(len(ctx)) if ctx[i:i + len(a)] == a]
    if not starts:
        return True
    i = starts[0]
    window = " ".join(ctx[max(0, i - WINDOW): i + len(a) + WINDOW])
    return window in " ".join(words(block_text))


def check(page: dict | None, link: dict) -> tuple[str, str, str]:
    """(status, link text found, note) for one link on a page as crawl.extract returns it; None for
    a page that did not load."""
    if page is None:
        return "page_error", "", ""
    target = norm_url(link["target_url"])
    found = [(b, text) for b in page.get("blocks", []) for t, text in b.get("anchors", []) if t == target]
    note = f"linked {len(found)} times on the page" if len(found) > 1 else ""
    if not found:
        if target in page.get("content_links", []):
            if not any("anchors" in b for b in page.get("blocks", [])):
                return "ok", "", "crawled before 0.3.0 - link text not checked"
            return "other_place", "", "in the content, outside the text blocks"
        if target in page.get("other_links", []) or target in page.get("breadcrumb_links", []):
            return "outside_content", "", ""
        return "missing", "", ""
    expected = " ".join(words(link["anchor"]))
    same_text = [(b, t) for b, t in found if " ".join(words(t)) == expected]
    if not same_text:
        return "other_text", found[0][1], note
    if any(in_passage(b["raw"], link["anchor"], link.get("context", "")) for b, _ in same_text):
        return "ok", same_text[0][1], note
    return "other_place", same_text[0][1], note


def check_pages(links: list[dict], pages: dict[str, dict], where: str) -> list[dict]:
    """Checks every link against already extracted pages (norm_url → page)."""
    checked = datetime.now().isoformat(timespec="minutes")
    out = []
    for link in links:
        status, text, note = check(pages.get(norm_url(link["source_url"])), link)
        out.append({**{k: link.get(k, "") for k in ("source_url", "anchor", "target_url")},
                    "status": status, "found_text": text, "note": note, "checked": checked, "where": where})
    return out


def summary(results: list[dict]) -> str:
    counts = Counter(r["status"] for r in results)
    return ", ".join(f"{s} {counts[s]}" for s in ["ok"] + PROBLEMS if counts[s])


def default_links(ddir: Path) -> Path:
    for name in ("links-for-agent.md", "links.csv"):
        if (ddir / name).exists():
            return ddir / name
    raise SystemExit(f"no links to check in {ddir} - run `internal-linking run` first or give --links")


def verify(domain: str, links_path: str | None = None, base_url: str | None = None, delay: float = 0.5) -> list[dict]:
    """Fetches the pages that should have links (live, or from base_url) and checks every link."""
    ddir = data_dir(domain)
    links = agent.read_links(links_path or default_links(ddir))
    live = f"https://{domain.removeprefix('https://').removeprefix('http://').strip('/')}"
    base = (base_url or "").rstrip("/")
    robots = None if base else load_robots(live)[0]

    def local(url: str) -> str:
        return base + agent.target_path(url) if base else url

    pages: dict[str, dict | None] = {}
    sources = list(dict.fromkeys(link["source_url"] for link in links))
    print(f"checking {len(links)} links on {len(sources)} pages ({base or live})...")
    for url in sources:
        if robots and not robots.can_fetch(ROBOTS_AGENT, url):
            print(f"  ! {url}: disallowed by robots.txt")
            pages[norm_url(url)] = None
            continue
        r = fetch(local(url))
        time.sleep(delay)
        if r is None or r.status_code != 200:
            pages[norm_url(url)] = None
            continue
        # a local preview may write absolute links with its own host: read them as the live site's
        html = r.text.replace(base + "/", live + "/") if base else r.text
        pages[norm_url(url)] = extract(url, html, domain)
    results = check_pages(links, pages, base or "live")

    targets: dict[str, str] = {}  # target → what is wrong with it ("" = loads)
    for res in results:
        if res["status"] in ("ok", "other_text", "other_place") and res["target_url"] not in targets:
            r = fetch(local(res["target_url"]))
            time.sleep(delay)
            if r is None or r.status_code != 200:
                targets[res["target_url"]] = f"target: status {getattr(r, 'status_code', 'error')}"
            elif norm_url(r.url) != norm_url(local(res["target_url"])):  # a trailing slash alone is no redirect
                targets[res["target_url"]] = f"target redirects to {agent.target_path(r.url)}"
            else:
                targets[res["target_url"]] = ""
        if targets.get(res["target_url"]) and res["status"] in ("ok", "other_text", "other_place"):
            res["status"], res["note"] = "target_error", targets[res["target_url"]]
    write_csv(ddir / "verify.csv", results, FIELDS)
    return results


def print_results(results: list[dict], path: Path) -> None:
    print(f"verified: {summary(results)} → {path}")
    for r in results:
        if r["status"] != "ok":
            found = f" (found “{r['found_text']}”)" if r["found_text"] else ""
            print(f"  {r['status']:15} {agent.target_path(r['source_url'])} “{r['anchor']}” → "
                  f"{agent.target_path(r['target_url'])}{found}{' - ' + r['note'] if r['note'] else ''}")


def verify_command(args) -> None:
    from . import report  # report reads verify.csv for its Verification tab

    results = verify(args.domain, args.links, args.base_url, args.delay)
    ddir = data_dir(args.domain)
    print_results(results, ddir / "verify.csv")
    if (ddir / "recommendations.csv").exists():
        report.rebuild(types.SimpleNamespace(config=args.config, domain=[args.domain], max_links=None))
    if any(r["status"] != "ok" for r in results):
        sys.exit(1)


def load(ddir: Path) -> list[dict]:
    return read_csv(ddir / "verify.csv") if (ddir / "verify.csv").exists() else []


def add_parser(sub) -> None:
    p = sub.add_parser("verify", help="check that the proposed links are on the site (live or a local preview)")
    p.add_argument("--domain", required=True, help="e.g. example.com")
    p.add_argument("--links", help="links-for-agent.md or a CSV (default: data/<domain>/links-for-agent.md)")
    p.add_argument("--base-url", help="check a local preview instead of the live site, e.g. http://localhost:4321")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    p.set_defaults(func=verify_command)
