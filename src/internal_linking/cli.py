"""Command line: internal-linking <command> ...

  internal-linking run --domain example.com
  internal-linking run --domains-file domains.txt --max-links 3
  internal-linking crawl --domain example.com
  internal-linking questions
  internal-linking sample --domain example.com
  internal-linking label --domain example.com
  internal-linking evaluate --domain a.com --domain b.com --write-config
"""
import argparse

from . import __version__, crawl, pipeline
from .common import set_data_root
from .jev import pairs
from .quality import evaluate, label, sample


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="internal-linking", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--data-dir", default="data", help="where results are stored (default: ./data)")
    ap.add_argument("--config", help="config file with thresholds (default: ./config.json, then built-in)")
    sub = ap.add_subparsers(dest="command", required=True)
    for module in (pipeline, crawl, pairs, sample, label, evaluate):
        module.add_parser(sub)
    args = ap.parse_args(argv)
    set_data_root(args.data_dir)
    args.func(args)


if __name__ == "__main__":
    main()
