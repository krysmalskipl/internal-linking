"""Krok 3: recommendations.csv → sample_to_label.csv

Losuje pary źródło → cel (jeszcze nieobecne w próbce) po równo z 5 kwantyli prawdopodobieństwa, żeby próbka miała
przykłady pewne i niepewne niezależnie od tego, jak rozkładają się wyniki Jev.
Wypełnij kolumnę `ok` (1 = dobry link, 0 = zły) i zapisz plik.

  python sample.py --domain example.com --n 40
"""
import argparse
import random

from common import data_dir, read_csv, write_csv

N_BINS = 5
FIELDS = ["ok", "probability", "source_title", "target_title", "anchor", "anchor_type", "in_menu", "source_url", "target_url", "rank"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    ddir = data_dir(args.domain)
    out = ddir / "sample_to_label.csv"
    # istniejące oceny zostają; losujemy tylko spośród par, których jeszcze nie ma w próbce
    existing = read_csv(out) if out.exists() else []
    seen = {(r["source_url"], r["target_url"]) for r in existing}

    rows = [r for r in read_csv(ddir / "recommendations.csv") if (r["source_url"], r["target_url"]) not in seen]
    if not rows:
        raise SystemExit("Wszystkie aktualne rekomendacje są już w próbce.")
    rng = random.Random(args.seed)
    rows.sort(key=lambda r: float(r["probability"]))
    buckets = [rows[len(rows) * k // N_BINS: len(rows) * (k + 1) // N_BINS] for k in range(N_BINS)]
    ranges = [f"{float(b[0]['probability']):.2f}-{float(b[-1]['probability']):.2f}" for b in buckets if b]
    for b in buckets:
        rng.shuffle(b)

    # po równo z każdego przedziału, a braki uzupełniane z przedziałów, w których zostało więcej
    picked = []
    while len(picked) < min(args.n, len(rows)):
        for b in buckets:
            if b and len(picked) < args.n:
                picked.append(b.pop())
    picked.sort(key=lambda r: -float(r["probability"]))

    for r in picked:
        r["ok"] = ""
    write_csv(out, existing + picked, FIELDS)
    print(f"{len(picked)} nowych par (razem {len(existing) + len(picked)}) → {out}\nprzedziały kwantylowe prawdopodobieństwa: {', '.join(ranges)}")
    print("Wpisz w kolumnie `ok` 1 (dobry link) lub 0 (zły) i uruchom calibrate.py.")


if __name__ == "__main__":
    main()
