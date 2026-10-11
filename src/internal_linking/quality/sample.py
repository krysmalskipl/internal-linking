"""Quality check, step 1: recommendations.csv from `run` → sample_to_label.csv.

Draws pairs not yet in the sample, evenly from 5 quantiles of the Jev score within each match type
(exact / partial), so the sample holds confident and uncertain suggestions of every kind. Each pair
keeps the passage Jev judged (`context`), so `label` shows the same spot. Existing labels are kept.
Label the pairs with `label` (or put 1 / 0 in the `ok` column), then run `evaluate`.
--anchor-type draws from one match type only, e.g. fanout pairs before they join the default thresholds.
"""
import random

from ..common import data_dir, read_csv, write_csv

N_BINS = 5
FIELDS = ["ok", "score", "similarity", "source_title", "target_title", "anchor", "anchor_type", "placement",
          "context", "in_menu", "source_url", "target_url", "passage_similarity", "subquery"]


def sample(domain: str, n: int = 40, seed: int = 42, anchor_type: str | None = None) -> None:
    ddir = data_dir(domain)
    out = ddir / "sample_to_label.csv"
    existing = read_csv(out) if out.exists() else []
    seen = {(r["source_url"], r["target_url"]) for r in existing}
    current = read_csv(ddir / "recommendations.csv")
    # samples drawn by older versions lack the judged passage: take it from the current results
    by_pair = {(r["source_url"], r["target_url"]): r for r in current}
    for r in existing:
        for field in ("context", "placement", "similarity"):
            if not r.get(field):
                r[field] = by_pair.get((r["source_url"], r["target_url"]), {}).get(field, "")

    rows = [r for r in current if (r["source_url"], r["target_url"]) not in seen
            and (not anchor_type or r.get("anchor_type") == anchor_type)]
    if not rows:
        raise SystemExit(f"every current {anchor_type + ' ' if anchor_type else ''}suggestion is already in the sample"
                         + (" - or the run had none (run with --fanout / --semantic)" if anchor_type in ("fanout", "semantic") else ""))
    rng = random.Random(seed)
    rows.sort(key=lambda r: float(r["score"] or 0))
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("anchor_type", ""), []).append(r)
    buckets = [g[len(g) * k // N_BINS: len(g) * (k + 1) // N_BINS] for g in groups.values() for k in range(N_BINS)]
    buckets = [b for b in buckets if b]
    ranges = [f"{float(rows[len(rows) * k // N_BINS]['score'] or 0):.2f}" for k in range(N_BINS)]
    for b in buckets:
        rng.shuffle(b)

    # evenly from every bucket; shortfalls are filled from buckets with rows left
    picked = []
    while len(picked) < min(n, len(rows)):
        for b in buckets:
            if b and len(picked) < n:
                picked.append(b.pop())
    picked.sort(key=lambda r: -float(r["score"] or 0))

    for r in picked:
        r["ok"] = ""
    write_csv(out, existing + picked, FIELDS)
    types = ", ".join(f"{t or 'other'} {sum(r.get('anchor_type', '') == t for r in picked)}" for t in groups)
    print(f"{len(picked)} new pairs ({len(existing) + len(picked)} total; {types}) → {out}\n"
          f"score quantile starts: {', '.join(ranges)}")
    print("Label them (internal-linking label) and run internal-linking evaluate.")


def add_parser(sub) -> None:
    p = sub.add_parser("sample", help="draw suggestions for manual labelling")
    p.add_argument("--domain", required=True)
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--anchor-type", choices=["exact", "partial", "fanout", "semantic"],
                   help="only this match type (e.g. fanout, to measure it on its own)")
    p.set_defaults(func=lambda args: sample(args.domain, args.n, args.seed, args.anchor_type))
