"""Command line: internal-linking <command> ...

  internal-linking ui                               # the tool in the browser (localhost)
  internal-linking run --domain example.com
  internal-linking run --domains-file domains.txt --max-links 3
  internal-linking report --domain example.com      # rebuild report.html from saved results
  internal-linking embed --domain example.com       # page embeddings (similarity, graph) + report
  internal-linking verify --domain example.com      # are the inserted links on the site? (--base-url for a preview)
  internal-linking crawl --domain example.com
  internal-linking questions
  internal-linking sample --domain example.com
  internal-linking label --domain example.com
  internal-linking evaluate --domain a.com --domain b.com --write-config
"""
import argparse
import sys
from pathlib import Path

import requests

from . import __version__, crawl, embeddings, pipeline, report, verify
from .ui import server as ui
from .common import set_data_root
from .jev import pairs
from .jev.client import short_error
from .quality import evaluate, label, sample


def setup_console() -> None:
    """UTF-8, line-buffered output everywhere - Windows consoles default to a legacy code page
    (printing "→" would crash) and some terminals buffer output, which makes a long run look stuck."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass


ISSUES = "https://github.com/krysmalskipl/internal-linking/issues"
# results a command needs → what makes them
NEEDS = {"pages.jsonl": "run `internal-linking run --domain {d}` first",
         "recommendations.csv": "run `internal-linking run --domain {d}` first",
         "sample_to_label.csv": "draw a sample first: `internal-linking sample --domain {d}`"}


def explain(e: BaseException) -> str:
    """One line a user can act on - the errors a run can meet are named, anything else is a bug to report."""
    if isinstance(e, FileNotFoundError) and e.filename:
        path = Path(e.filename)
        hint = NEEDS.get(path.name, "").format(d=path.parent.name)
        return f"no such file: {e.filename}" + (f" - {hint}" if hint else "")
    if isinstance(e, requests.RequestException):
        return f"network error: {short_error(e)}"
    if isinstance(e, PermissionError) and e.filename:
        return f"no permission to use {e.filename}"
    if isinstance(e, (RuntimeError, OSError)):  # our own messages (missing key, no sitemap...) and file system errors
        return str(e)
    return (f"unexpected error ({type(e).__name__}: {e}) - run the command again with --debug "
            f"and report it at {ISSUES}")


def main(argv: list[str] | None = None) -> None:
    setup_console()
    ap = argparse.ArgumentParser(prog="internal-linking", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--data-dir", default="data", help="where results are stored (default: ./data)")
    ap.add_argument("--config", help="config file with thresholds (default: ./config.json, then built-in)")
    ap.add_argument("--debug", action="store_true", help="show the full error trace")
    sub = ap.add_subparsers(dest="command", required=True)
    for module in (pipeline, ui, verify, report, embeddings, crawl, pairs, sample, label, evaluate):
        module.add_parser(sub)
    args = ap.parse_args(argv)
    set_data_root(args.data_dir)
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n! stopped (Ctrl+C) - answers received so far are cached", file=sys.stderr)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 - every error ends with a message, not a trace
        if args.debug:
            raise
        print(f"! {explain(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
