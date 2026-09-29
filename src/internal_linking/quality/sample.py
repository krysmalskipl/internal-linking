"""Quality check, step 1: recommendations.csv from `run` → sample_to_label.csv.

Draws pairs not yet in the sample, evenly from 5 quantiles of the Jev score, so the sample holds
both confident and uncertain suggestions. Existing labels are kept. Label the pairs with `label`
(or put 1 / 0 in the `ok` column), then run `evaluate`.
"""
import random

from ..common import data_dir, read_csv, write_csv

N_BINS = 5
FIELDS = ["ok", "score", "source_title", "target_title", "anchor", "anchor_type", "miejsce", "kontekst",
          "in_menu", "source_url", "target_url"]


def sample(domain: str, n: int = 40, seed: int = 42) -> None:
    ddir = data_dir(domain)
    out = ddir / "sample_to_label.csv"
    existing = read_csv(out) if out.exists() else []
    seen = {(r["source_url"], r["target_url"]) for r in existing}

    rows = [r for r in read_csv(ddir / "recommendations.csv") if (r["source_url"], r["target_url"]) not in seen]
    if not rows:
        raise SystemExit("every current suggestion is already in the sample")
    rng = random.Random(seed)
    rows.sort(key=lambda r: float(r["score"] or 0))
    buckets = [rows[len(rows) * k // N_BINS: len(rows) * (k + 1) // N_BINS] for k in range(N_BINS)]
    ranges = [f"{float(b[0]['score'] or 0):.2f}-{float(b[-1]['score'] or 0):.2f}" for b in buckets if b]
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
    print(f"{len(picked)} new pairs ({len(existing) + len(picked)} total) → {out}\n"
          f"score quantiles: {', '.join(ranges)}")
    print("Label them (internal-linking label) and run internal-linking evaluate.")


def add_parser(sub) -> None:
    p = sub.add_parser("sample", help="draw suggestions for manual labelling")
    p.add_argument("--domain", required=True)
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--seed", type=int, default=42)
    p.set_defaults(func=lambda args: sample(args.domain, args.n, args.seed))
