"""links-for-agent.md: the links to insert, with instructions for a coding agent (Claude Code,
Antigravity, Cursor...) working in the site's source - Astro, Next.js/MDX, Hugo, Eleventy or plain
HTML. Links are grouped by the page that gets them, each with its exact phrase, target and the
passage it sits in, so the agent can find the right file by searching for the text. The file ends
with a CSV block of the same links that `internal-linking verify` reads to check the result.

`run` writes it for the accepted links; the report's "For AI agent (.md)" button builds the same
file for the current selection (report.py gets GUIDELINES from here).
"""
import csv
import io
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .common import now, shown

VERIFY_FIELDS = ["source_url", "anchor", "target_url", "context"]
GUIDELINES = """## Instructions for the coding agent

You are working in the source code of {domain} - for example an Astro, Next.js (MDX), Hugo or Eleventy project, or plain HTML. Insert every internal link listed below. Each link uses a phrase that already exists in an existing paragraph: do not write new text.

For each link:

1. **Find the source file.** Search the repository for a few consecutive words from the passage (5-8 words from its middle work best: Markdown formatting or line breaks may split longer runs, and "…" only marks where the passage was trimmed). The page URL and title are hints: the text usually lives in Markdown/MDX content files (e.g. `src/content/`, `content/`, `posts/`), sometimes in `.astro`, `.html`, `.jsx` or `.tsx` templates.
2. **Find the link text in that passage** and turn its first occurrence there into a link. Keep the words, their case, the punctuation and the surrounding formatting exactly as they are.
3. **Use the file's own link syntax:** Markdown/MDX `[link text](/path/)`; `.astro` and `.html` `<a href="/path/">link text</a>`; JSX/TSX the component the file already uses for internal links (e.g. `<Link href="/path/">`), otherwise `<a>`.
4. **Link to the target as a root-relative path** (`/path/` as listed), with or without a trailing slash as the project's existing internal links do.
5. **Skip a link and say why** when: the passage or the link text in it cannot be found; the link text is already inside a link; it is in a heading, code, an image alt text, front matter or a component prop; or the text comes from data you cannot safely edit (a CMS, an API, a generated file).
6. **Do not add other links or change anything else.**
7. **When you are done:** reply with a table - file, link text, target, status (inserted / skipped + reason); run the project's build or type check if it has one; then check the result with `internal-linking verify --domain {domain} --links <this file> --base-url <local preview URL>`, or without `--base-url` once deployed."""


def guidelines(domain: str) -> str:
    return GUIDELINES.replace("{domain}", domain)


def thresholds(cfg: dict, max_links: int) -> str:
    """The selection the links come from, in the words of the report's filters."""
    return (f"thresholds: score ≥ {round(cfg['score_min'] * 100)}%, context ≥ {round(cfg['context_min'] * 100)}%, "
            f"anchor ≥ {round(cfg['anchor_min'] * 100)}%, cannibalisation < {round(cfg['cannibalisation_max'] * 100)}%, "
            f"at most {max_links} per page"
            + (f", page similarity ≥ {round(cfg['similarity_min'] * 100)}%" if cfg.get("similarity_min") else ""))


def target_path(url: str) -> str:
    p = urlsplit(url)
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


def agent_markdown(domain: str, rows: list[dict], summary: str = "", generated: str | None = None) -> str:
    """The .md for a coding agent: header (with when it was generated), instructions, links grouped
    by source page, CSV block."""
    by_source: dict[str, list[dict]] = {}
    for r in rows:
        by_source.setdefault(r["source_url"], []).append(r)
    lines = [f"# Internal links to insert - {domain}", "",
             f"{len(rows)} link{'s' if len(rows) != 1 else ''} on {len(by_source)} page{'s' if len(by_source) != 1 else ''}"
             f" · generated {shown(generated or now())} by internal-linking {__version__}" + (f" · {summary}" if summary else ""),
             "", guidelines(domain), "", "## Links", ""]
    for source, links in by_source.items():
        lines += [f"### {target_path(source)} - {links[0].get('source_title', '')}", "", f"Page: {source}", ""]
        for n, r in enumerate(links, 1):
            lines += [f"{n}. Link text: `{r['anchor']}` → `{target_path(r['target_url'])}` - {r.get('target_title', '')}",
                      f"   > {' '.join(str(r.get('context', '')).split())}", ""]
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=VERIFY_FIELDS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows({k: " ".join(str(r.get(k, "")).split()) for k in VERIFY_FIELDS} for r in rows)
    lines += ["## For `internal-linking verify` - do not edit", "", "```csv", out.getvalue().rstrip("\n"), "```", ""]
    return "\n".join(lines)


def link_files(folder: Path, domain: str = "") -> list[Path]:
    """Link lists downloaded from the report (links-for-agent-<domain>-<time>.md, links-<domain>-….csv)
    in a folder, newest first."""
    names = [f"links-for-agent-{domain}*.md", f"links-{domain}*.csv"] if domain else ["links-for-agent-*.md", "links-*.csv"]
    found = {p for name in names for p in folder.glob(name)} if folder.is_dir() else set()
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def read_links(path: str | Path) -> list[dict]:
    """The links to check: a CSV with source_url, anchor and target_url (links.csv, the report's CSV
    export) or the CSV block at the end of a links-for-agent.md."""
    path = Path(path).expanduser()
    if not path.is_file():
        near = link_files(path.parent)[:5]
        raise RuntimeError(f"no such file: {path}" + ("\nlink lists in that folder, newest first:\n" +
                                                       "\n".join(f"  {p}" for p in near) if near else ""))
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".md":
        m = re.search(r"```csv\n(.*?)\n```", text, re.S)
        if not m:
            raise RuntimeError(f"{path}: no ```csv block - is it a links-for-agent.md from internal-linking?")
        text = m.group(1)
    rows = list(csv.DictReader(io.StringIO(text)))
    missing = {"source_url", "anchor", "target_url"} - set(rows[0] if rows else VERIFY_FIELDS)
    if missing:
        raise RuntimeError(f"{path}: missing columns {', '.join(sorted(missing))}")
    return rows
