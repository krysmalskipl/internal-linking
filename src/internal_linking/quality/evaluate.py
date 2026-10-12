"""Quality check, step 3: measure the signals on hand labels and tune the thresholds.

Joins labelled pairs from every given domain (sample_to_label.csv, column ok = 1/0) with the
current `run` output (recommendations.csv). Prints the AUC of each signal - the Jev answers and the
page similarity from the embeddings - with a 90% bootstrap interval (1.0 = separates good links
from bad perfectly, 0.5 = random; a wide interval means more labels are needed), and the precision
of the current thresholds per match type. Then grid-searches the thresholds: the combination that
accepts the most good links at precision ≥ target, never going below --score-floor. A similarity
threshold is only set when it improves on none. The thresholds are shared by all domains;
--write-config saves them to ./config.json (or --config).

Pairs found by meaning (fanout, semantic - see fanout.py) are measured on their own - their
precision under the current thresholds and the AUC of the passage similarity - and stay out of the
threshold search until they have earned a place in the defaults.
"""
import itertools

import numpy as np
from scipy.stats import rankdata

from ..common import data_dir, read_csv
from ..config import load_config, save_config

YES, NO = {"1", "tak", "t", "y", "yes", "ok"}, {"0", "nie", "n", "no"}
SIGNALS = ["score", "jev_context", "jev_anchor", "jev_value", "jev_cannibalisation", "similarity"]
LOWER_IS_BETTER = {"jev_cannibalisation"}
BY_MEANING = {"fanout", "semantic"}
MEANING_SIGNALS = ["score", "jev_context", "jev_anchor", "passage_similarity", "similarity"]
GRID = {
    "score_min": [0.8, 0.85, 0.9, 0.93, 0.95, 0.97],
    "context_min": [0.5, 0.6, 0.7, 0.75, 0.8, 0.85],
    "anchor_min": [0.5, 0.6, 0.7, 0.8],
}
MIN_LABELS = 10


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


def value(r: dict, signal: str) -> float | None:
    try:
        return float(r.get(signal, ""))
    except ValueError:
        return None  # e.g. no similarity: the run had no embeddings


def auc(y: np.ndarray, s: np.ndarray) -> float:
    """ROC AUC from ranks (Mann-Whitney U): the chance a good link scores higher than a bad one."""
    pos = y.sum()
    return float((rankdata(s)[y == 1].sum() - pos * (pos + 1) / 2) / (pos * (len(y) - pos)))


def auc_interval(y: np.ndarray, s: np.ndarray, n: int = 1000, seed: int = 0) -> tuple[float, float, float] | None:
    """AUC and its 90% bootstrap interval; None when there are too few labels of either kind."""
    if len(y) < MIN_LABELS or len(set(y)) < 2:
        return None
    rng, aucs = np.random.default_rng(seed), []
    for _ in range(n):
        i = rng.integers(0, len(y), len(y))
        if 0 < y[i].sum() < len(i):
            aucs.append(auc(y[i], s[i]))
    lo, hi = np.percentile(aucs, [5, 95])
    return auc(y, s), lo, hi


def passes(r: dict, cfg: dict) -> bool:
    similarity = value(r, "similarity")
    return (float(r["score"]) >= cfg["score_min"] and float(r["jev_context"]) >= cfg["context_min"]
            and float(r["jev_anchor"]) >= cfg["anchor_min"]
            and float(r["jev_cannibalisation"]) < cfg["cannibalisation_max"]
            and (similarity is None or similarity >= cfg.get("similarity_min", 0)))


def auc_table(labelled: list[tuple[dict, bool]], signals: list[str]) -> None:
    print(f"\n{'signal':20} {'AUC (90% interval)':22} labelled pairs")
    for sig in signals:
        have = [(value(r, sig), ok) for r, ok in labelled if value(r, sig) is not None]
        ys = np.array([ok for _, ok in have], dtype=int)
        s = np.array([v for v, _ in have]) * (-1 if sig in LOWER_IS_BETTER else 1)
        res = auc_interval(ys, s)
        auc = f"{res[0]:.2f} ({res[1]:.2f}-{res[2]:.2f})" if res else "n/a"
        print(f"{sig:20} {auc:22} {len(have)}")


def by_meaning_report(labelled: list[tuple[dict, bool]], cfg: dict) -> None:
    """Fan-out and semantic pairs: precision under the current thresholds and their signals."""
    print(f"\nfan-out and semantic pairs (not part of the threshold search): {len(labelled)} labelled, "
          f"{sum(ok for _, ok in labelled)} good")
    for kind in sorted({r.get("anchor_type", "") for r, _ in labelled}):
        accepted = [ok for r, ok in labelled if r.get("anchor_type") == kind and passes(r, cfg)]
        print(f"  {kind:8} precision of the current thresholds: "
              + (f"{sum(accepted)}/{len(accepted)} good ({sum(accepted) / len(accepted):.0%})" if accepted else "none accepted"))
    auc_table(labelled, MEANING_SIGNALS)


def evaluate(args) -> None:
    labelled_all = load_labelled(args.domain)
    meaning = [(r, ok) for r, ok in labelled_all if r.get("anchor_type") in BY_MEANING]
    labelled = [(r, ok) for r, ok in labelled_all if r.get("anchor_type") not in BY_MEANING]
    base = load_config(args.config)
    y = np.array([ok for _, ok in labelled], dtype=int)
    if len(y) < MIN_LABELS or len(set(y)) < 2:
        if meaning:
            by_meaning_report(meaning, base)
            print(f"\nphrase-matched pairs: not enough labels to tune the thresholds (need ≥ {MIN_LABELS}, both good and bad)")
            return
        raise SystemExit(f"not enough labels (need ≥ {MIN_LABELS}, both good and bad)")

    auc_table(labelled, SIGNALS)

    print("\nprecision of the current thresholds:")
    for kind in sorted({r.get("anchor_type", "") for r, _ in labelled}):
        accepted = [ok for r, ok in labelled if r.get("anchor_type", "") == kind and passes(r, base)]
        print(f"  {kind or 'other':8} {sum(accepted)}/{len(accepted)} good" if accepted else f"  {kind or 'other':8} none accepted")

    sims = [v for r, _ in labelled if (v := value(r, "similarity")) is not None]
    # absolute similarities depend on the model, so the candidates come from the labelled pairs
    grid = {**GRID, "similarity_min": sorted({0.0, *(round(float(q), 3) for q in np.quantile(sims, [0.1, 0.2, 0.3]))})
            if len(sims) >= MIN_LABELS else [0.0]}
    best = None
    for values in itertools.product(*grid.values()):
        cfg = {**base, **dict(zip(grid, values))}
        if cfg["score_min"] < args.score_floor:
            continue
        accepted = [ok for r, ok in labelled if passes(r, cfg)]
        if not accepted:
            continue
        precision, good = sum(accepted) / len(accepted), sum(accepted)
        # precision ≥ target first, then the most good links, then the strictest Jev thresholds -
        # and the lowest similarity threshold, so it is only set when it helps
        met = precision >= args.target_precision
        key = (met, good if met else precision, precision, sum(values[:len(GRID)]), -cfg["similarity_min"])
        if best is None or key > best[0]:
            best = (key, cfg, precision, good, len(accepted))

    _, cfg, precision, good, n = best
    print("\nbest thresholds: " + ", ".join(f"{k} {cfg[k]}" for k in grid))
    print(f"on labelled pairs: {n} accepted, {good} good → precision {precision:.0%}, "
          f"recall {good}/{int(y.sum())} good links")
    if precision < args.target_precision:
        print(f"note: no combination reaches {args.target_precision:.0%} - this is the best available")
    if args.write_config:
        print(f"saved → {save_config(cfg, args.config)}")
    if meaning:
        by_meaning_report(meaning, base)


def add_parser(sub) -> None:
    p = sub.add_parser("evaluate", help="signal AUC on hand labels and threshold tuning")
    p.add_argument("--domain", action="append", required=True, help="labelled domain (repeatable)")
    p.add_argument("--target-precision", type=float, default=0.9)
    p.add_argument("--score-floor", type=float, default=0.9, help="never set score_min below this")
    p.add_argument("--write-config", action="store_true", help="save the thresholds to ./config.json")
    p.set_defaults(func=evaluate)
