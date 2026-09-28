"""Krok 4: ocenione sample_to_label.csv → próg → final.csv

Dla progów prawdopodobieństwa liczy precyzję (ile rekomendacji powyżej progu oceniłeś jako
dobre) i pokrycie (jaka część próbki jest powyżej progu). Wybiera najniższy próg, który
osiąga docelową precyzję, i oznacza rekomendacje jako auto_accept albo review.

  python calibrate.py --domain example.com --target-precision 0.9
"""
import argparse

from common import data_dir, read_csv, write_csv

YES, NO = {"1", "tak", "t", "y", "yes", "ok"}, {"0", "nie", "n", "no"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--labeled", help="plik z ocenami (domyślnie data/<domena>/sample_to_label.csv)")
    ap.add_argument("--target-precision", type=float, default=0.9)
    ap.add_argument("--min-support", type=int, default=5,
                    help="minimalna liczba ocenionych par powyżej progu, żeby mu ufać")
    args = ap.parse_args()

    ddir = data_dir(args.domain)
    labeled = [r for r in read_csv(ddir / "sample_to_label.csv" if not args.labeled else args.labeled)
               if r["ok"].strip().lower() in YES | NO]
    if not labeled:
        raise SystemExit("Brak ocen w kolumnie `ok` (1/0).")
    pairs = [(float(r["probability"]), r["ok"].strip().lower() in YES) for r in labeled]
    print(f"ocenionych par: {len(pairs)}, dobrych: {sum(ok for _, ok in pairs)}\n")

    print("  próg  precyzja  pokrycie  par")
    chosen = None
    # kandydaci na próg = wartości z próbki, bo skala zależy od liczby opcji w pytaniu
    for t in sorted({p for p, _ in pairs}):
        above = [ok for p, ok in pairs if p >= t]
        precision, coverage = sum(above) / len(above), len(above) / len(pairs)
        hit = precision >= args.target_precision and len(above) >= args.min_support
        if hit and chosen is None:
            chosen = t
        print(f" {t:.3f}  {precision:6.1%}   {coverage:6.1%}  {len(above):4d}{'  ←' if chosen == t else ''}")

    if chosen is None:
        print(f"\nŻaden próg nie daje precyzji ≥ {args.target_precision:.0%} przy ≥ {args.min_support} parach. "
              "Wszystko idzie do ręcznego przeglądu - rozważ większą próbkę lub niższą precyzję docelową.")
    else:
        print(f"\nwybrany próg: {chosen:.3f}")

    labels = {(r["source_url"], r["target_url"]): r["ok"].strip().lower() in YES for r in labeled}
    rows = read_csv(ddir / "recommendations.csv")
    for r in rows:
        key = (r["source_url"], r["target_url"])
        if key in labels:
            r["decision"] = "accept (ręcznie)" if labels[key] else "reject (ręcznie)"
        elif chosen is not None and float(r["probability"]) >= chosen:
            r["decision"] = "auto_accept"
        else:
            r["decision"] = "review"
    order = {"accept (ręcznie)": 0, "auto_accept": 1, "review": 2, "reject (ręcznie)": 3}
    rows.sort(key=lambda r: (order[r["decision"]], -float(r["probability"])))

    out = ddir / "final.csv"
    write_csv(out, rows, ["decision"] + [k for k in rows[0] if k != "decision"])
    summary = {d: sum(r["decision"] == d for r in rows) for d in order}
    print(f"{out}: " + ", ".join(f"{d} {n}" for d, n in summary.items()))


if __name__ == "__main__":
    main()
