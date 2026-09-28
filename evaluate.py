"""Kontrola jakości i ustawienie progów w config.json na podstawie ręcznych ocen.

Zbiera ocenione pary ze wszystkich podanych domen (sample_to_label.csv, kolumna ok = 1/0) i łączy
je z aktualnymi wynikami run.py (recommendations.csv). Pokazuje AUC każdego sygnału Jev
(1,0 = idealnie oddziela dobre linki od złych, 0,5 = losowo) i przeszukuje kombinacje progów:
wybiera tę, która przy precyzji ≥ docelowej przyjmuje najwięcej dobrych linków, nigdy nie schodząc
z oceną poniżej --score-floor. Progi są wspólne dla wszystkich domen.

  python evaluate.py --domain a.pl --domain b.pl                  # tylko raport
  python evaluate.py --domain a.pl --domain b.pl --write-config   # zapis progów
"""
import argparse
import itertools
import json

import numpy as np
from sklearn.metrics import roc_auc_score

from common import ROOT, data_dir, read_csv

YES, NO = {"1", "tak", "t", "y", "yes", "ok"}, {"0", "nie", "n", "no"}
SIGNALS = ["score", "jev_kontekst", "jev_anchor", "jev_wartosc", "jev_kanibalizacja"]
GRID = {
    "score_min": [0.8, 0.85, 0.9, 0.93, 0.95, 0.97],
    "kontekst_min": [0.5, 0.6, 0.7, 0.75, 0.8, 0.85],
    "anchor_min": [0.5, 0.6, 0.7, 0.8],
}


def load_labeled(domains: list[str]) -> list[tuple[dict, bool]]:
    out = []
    for d in domains:
        ddir = data_dir(d)
        labels = {(r["source_url"], r["target_url"]): r["ok"].strip().lower() in YES
                  for r in read_csv(ddir / "sample_to_label.csv") if r["ok"].strip().lower() in YES | NO}
        found = [(r, labels[(r["source_url"], r["target_url"])]) for r in read_csv(ddir / "recommendations.csv")
                 if (r["source_url"], r["target_url"]) in labels and r.get("score")]
        print(f"{d}: {len(found)} ocenionych par w aktualnych wynikach (dobrych: {sum(ok for _, ok in found)})")
        out += found
    return out


def passes(r: dict, cfg: dict) -> bool:
    return (float(r["score"]) >= cfg["score_min"] and float(r["jev_kontekst"]) >= cfg["kontekst_min"]
            and float(r["jev_anchor"]) >= cfg["anchor_min"]
            and float(r["jev_kanibalizacja"]) < cfg["kanibalizacja_max"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", action="append", required=True)
    ap.add_argument("--target-precision", type=float, default=0.9)
    ap.add_argument("--score-floor", type=float, default=0.9, help="najniższy dopuszczalny próg oceny")
    ap.add_argument("--write-config", action="store_true")
    args = ap.parse_args()

    labeled = load_labeled(args.domain)
    y = np.array([ok for _, ok in labeled], dtype=int)
    if len(y) < 10 or len(set(y)) < 2:
        raise SystemExit("Za mało ocen (potrzeba ≥ 10, w tym dobre i złe).")

    print(f"\n{'sygnał':18} AUC")
    for sig in SIGNALS:
        s = np.array([float(r[sig]) for r, _ in labeled])
        auc = roc_auc_score(y, -s if sig == "jev_kanibalizacja" else s)
        print(f"{sig:18} {auc:.2f}")

    base = json.loads((ROOT / "config.json").read_text())
    best = None
    for values in itertools.product(*GRID.values()):
        cfg = {**base, **dict(zip(GRID, values))}
        if cfg["score_min"] < args.score_floor:
            continue
        accepted = [ok for r, ok in labeled if passes(r, cfg)]
        if not accepted:
            continue
        precision, good = sum(accepted) / len(accepted), sum(accepted)
        # najpierw precyzja ≥ docelowej, potem najwięcej dobrych, potem najostrzejsze progi
        key = (precision >= args.target_precision, good if precision >= args.target_precision else precision,
               precision, sum(values))
        if best is None or key > best[0]:
            best = (key, cfg, precision, good, len(accepted))

    _, cfg, precision, good, n = best
    print(f"\nnajlepsze progi: " + ", ".join(f"{k} {cfg[k]}" for k in GRID))
    print(f"na ocenionych parach: przyjęte {n}, w tym dobrych {good} → precyzja {precision:.0%}, "
          f"przyjęto {good}/{int(y.sum())} dobrych")
    if precision < args.target_precision:
        print(f"uwaga: żadna kombinacja nie osiąga {args.target_precision:.0%} - to najlepsza możliwa")
    if args.write_config:
        (ROOT / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        print(f"zapisano → {ROOT / 'config.json'}")


if __name__ == "__main__":
    main()
