"""Quality check, step 3: measure the Jev signals on hand labels and tune the thresholds.

Joins labelled pairs from every given domain (sample_to_label.csv, column ok = 1/0) with the
current `run` output (recommendations.csv). Prints the AUC of each Jev signal (1.0 = separates
good links from bad perfectly, 0.5 = random) and grid-searches the thresholds: the combination that
accepts the most good links at precision ≥ target, never going below --score-floor. The thresholds
are shared by all domains; --write-config saves them to ./config.json (or --config).
"""
import itertools

import numpy as np
from sklearn.metrics import roc_auc_score

from ..common import data_dir, read_csv
from ..config import load_config, save_config

YES, NO = {"1", "tak", "t", "y", "yes", "ok"}, {"0", "nie", "n", "no"}
SIGNALS = ["score", "jev_context", "jev_anchor", "jev_value", "jev_cannibalisation"]
GRID = {
    "score_min": [0.8, 0.85, 0.9, 0.93, 0.95, 0.97],
    "context_min": [0.5, 0.6, 0.7, 0.75, 0.8, 0.85],
    "anchor_min": [0.5, 0.6, 0.7, 0.8],
}


def load_labelled(domains: list[str]) -> list[tuple[dict, bool]]:
    out = []
    for d in domains:
        ddir = data_dir(d)
        labels = {(r["source_url"], r["target_url"]): r["ok"].strip().lower() in YES
                  for r in read_csv(ddir / "sample_to_label.csv") if r["ok"].strip().lower() in YES | NO}
        found = [(r, labels[(r["source_url"], r["target_url"])]) for r in read_csv(ddir / "recommendations.csv")
                 if (r["source_url"], r["target_url"]) in labels and r.get("score")]
        print(f"{d}: {len(found)} labelled pairs in the current output ({sum(ok for _, ok in found)} good)")
        out += found
    return out


def passes(r: dict, cfg: dict) -> bool:
    return (float(r["score"]) >= cfg["score_min"] and float(r["jev_context"]) >= cfg["context_min"]
            and float(r["jev_anchor"]) >= cfg["anchor_min"]
            and float(r["jev_cannibalisation"]) < cfg["cannibalisation_max"])


def evaluate(args) -> None:
    labelled = load_labelled(args.domain)
    y = np.array([ok for _, ok in labelled], dtype=int)
    if len(y) < 10 or len(set(y)) < 2:
        raise SystemExit("not enough labels (need ≥ 10, both good and bad)")

    print(f"\n{'signal':20} AUC")
    for sig in SIGNALS:
        s = np.array([float(r[sig]) for r, _ in labelled])
        print(f"{sig:20} {roc_auc_score(y, -s if sig == 'jev_cannibalisation' else s):.2f}")

    base = load_config(args.config)
    best = None
    for values in itertools.product(*GRID.values()):
        cfg = {**base, **dict(zip(GRID, values))}
        if cfg["score_min"] < args.score_floor:
            continue
        accepted = [ok for r, ok in labelled if passes(r, cfg)]
        if not accepted:
            continue
        precision, good = sum(accepted) / len(accepted), sum(accepted)
        # precision ≥ target first, then the most good links, then the strictest thresholds
        met = precision >= args.target_precision
        key = (met, good if met else precision, precision, sum(values))
        if best is None or key > best[0]:
            best = (key, cfg, precision, good, len(accepted))

    _, cfg, precision, good, n = best
    print("\nbest thresholds: " + ", ".join(f"{k} {cfg[k]}" for k in GRID))
    print(f"on labelled pairs: {n} accepted, {good} good → precision {precision:.0%}, "
          f"recall {good}/{int(y.sum())} good links")
    if precision < args.target_precision:
        print(f"note: no combination reaches {args.target_precision:.0%} - this is the best available")
    if args.write_config:
        print(f"saved → {save_config(cfg, args.config)}")


def add_parser(sub) -> None:
    p = sub.add_parser("evaluate", help="signal AUC on hand labels and threshold tuning")
    p.add_argument("--domain", action="append", required=True, help="labelled domain (repeatable)")
    p.add_argument("--target-precision", type=float, default=0.9)
    p.add_argument("--score-floor", type=float, default=0.9, help="never set score_min below this")
    p.add_argument("--write-config", action="store_true", help="save the thresholds to ./config.json")
    p.set_defaults(func=evaluate)
