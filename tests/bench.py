"""Benchmark: a real run replayed without network, against hand labels.

A benchmark folder holds what a `run` left in data/<domain>/ - pages.jsonl and the Jev answer
cache jev_pairs.jsonl, plain or gzipped - plus labels.csv: one row per judged pair (source_url, target_url, anchor,
ok = 1/0, why = a short reason code for a 0). Replaying the run with the current code shows what a
change does to the suggestions: which labelled pairs are still found, how many Jev calls a run would
need, and how precise the links picked with the given thresholds are. Pairs whose phrase changed are
not in the answer cache; they are counted as "to judge" and their (source, target) label is shown.

    python tests/bench.py tests/benchmark/krysmalski.pl [more folders] [--score 0.9 --anchor 0.6 ...]
"""
import argparse
import csv
import gzip
import json
import sys
from pathlib import Path

from internal_linking import pipeline
from internal_linking.common import read_jsonl
from internal_linking.config import load_config
from internal_linking.jev import pairs

LEGACY = pairs.LEGACY_VERSION  # the answers were cached under jev-1.13


def jsonl(folder: Path, name: str) -> list[dict]:
    if (folder / name).exists():
        return read_jsonl(folder / name)
    with gzip.open(folder / f"{name}.gz", "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def labels(folder: Path) -> dict[tuple[str, str, str], tuple[bool, str]]:
    with open(folder / "labels.csv", encoding="utf-8") as f:
        return {(r["source_url"], r["target_url"], r["anchor"]): (r["ok"] == "1", r.get("why", "")) for r in csv.DictReader(f)}


def replay(folder: Path, cfg: dict) -> dict:
    """Candidates from the saved pages, Jev answers from the cache only, then the usual selection."""
    pages = jsonl(folder, "pages.jsonl")
    cache = {r["key"]: r["response"] for r in jsonl(folder, "jev_pairs.jsonl")}
    rows, _ = pipeline.find_candidates(pages, cfg)
    to_judge = 0
    for r in rows:
        answer = cache.get(pairs.cache_key(r["_state"], r["lang"], LEGACY))
        if answer is not None and pairs.well_formed(answer, r["lang"]):
            r.update(pairs.summarize(answer, r["lang"]))
        else:
            to_judge += 1
    pipeline.select(rows, cfg, cfg["max_links_per_page"])
    labelled = labels(folder)
    by_pair = {(s, t): ok for (s, t, _), (ok, _) in labelled.items()}
    found = {(r["source_url"], r["target_url"], r["anchor"]) for r in rows}
    accepted = [r for r in rows if r["decision"] == "accept"]
    judged_accepted = [r for r in accepted if (r["source_url"], r["target_url"], r["anchor"]) in labelled]
    good = sum(labelled[(r["source_url"], r["target_url"], r["anchor"])][0] for r in judged_accepted)
    lost_good = [k for k, (ok, _) in labelled.items() if ok and k not in found
                 and not any((r["source_url"], r["target_url"]) == k[:2] for r in rows)]
    rephrased = [(r["source_url"], r["target_url"], r["anchor"], by_pair[(r["source_url"], r["target_url"])])
                 for r in rows if (r["source_url"], r["target_url"], r["anchor"]) not in labelled
                 and (r["source_url"], r["target_url"]) in by_pair]
    return {
        "site": folder.name, "pairs": len(rows), "to_judge": to_judge,
        "labelled_pairs_found": len(found & labelled.keys()), "labelled_pairs": len(labelled),
        "good_labelled": sum(ok for ok, _ in labelled.values()),
        "accepted": len(accepted), "accepted_labelled": len(judged_accepted), "accepted_good": good,
        "precision": good / len(judged_accepted) if judged_accepted else None,
        "lost_good": lost_good, "rephrased": rephrased,
        "dropped_bad": sum(1 for k, (ok, _) in labelled.items() if not ok and k not in found),
    }


def replay_meaning(folder: Path, cfg: dict) -> dict | None:
    """Fan-out and semantic pairs: the candidates a run found (meaning_candidates.jsonl - which
    passage, which target, the sub-query; finding them again would need every paragraph's embedding),
    then the current code from there - the phrases offered, Jev's link-text choice and judgement from
    the caches, the thresholds. labels_meaning.csv holds a hand label per (source, target) pair."""
    if not (folder / "meaning_candidates.jsonl.gz").exists():
        return None
    from internal_linking import fanout, passages

    site = pipeline.prepare(jsonl(folder, "pages.jsonl"), cfg)
    index = {p["url"]: i for i, p in enumerate(site["pages"])}
    cands = [{"i": index[c["source_url"]], "j": index[c["target_url"]], "b": c["block"], "query": c["query"],
              "sim": c["sim"], "kind": c["kind"]} for c in jsonl(folder, "meaning_candidates.jsonl")]
    anchors = {r["key"]: r["response"] for r in jsonl(folder, "jev_anchors.jsonl")}
    answers = {r["key"]: r["response"] for r in jsonl(folder, "meaning_jev_pairs.jsonl")}
    pages, titles, to_ask = site["pages"], site["titles"], 0
    for c in cands:
        src, tgt = pages[c["i"]], pages[c["j"]]
        c["phrases"] = passages.phrase_candidates(src["blocks"][c["b"]]["text"], src["lang"],
                                                  " ".join((c["query"], titles[c["j"]], tgt["h1"])))
        c["phrase"] = None
        if c["phrases"]:
            state = pairs.anchor_state(src, titles[c["i"]], src["blocks"], c["b"], tgt, titles[c["j"]],
                                       c["query"] or titles[c["j"]], src["lang"])
            resp = anchors.get(pairs.anchor_cache_key(state, pairs.anchor_questions(c["phrases"], src["lang"]), LEGACY))
            if resp is not None and pairs.anchor_well_formed(resp, c["phrases"]):
                c["phrase"] = pairs.chosen_anchor(resp, c["phrases"])[0]
            else:
                to_ask += 1
    rows = fanout.to_rows(cands, site)
    for r in rows:
        answer = answers.get(pairs.cache_key(r["_state"], r["lang"], LEGACY))
        if answer is not None and pairs.well_formed(answer, r["lang"]):
            r.update(pairs.summarize(answer, r["lang"]))
        else:
            to_ask += 1
    pipeline.select(rows, cfg, cfg["max_links_per_page"])
    with open(folder / "labels_meaning.csv", encoding="utf-8") as f:
        labelled = {(r["source_url"], r["target_url"]): r["ok"] == "1" for r in csv.DictReader(f)}
    accepted = [r for r in rows if r["decision"] == "accept"]
    judged = [r for r in accepted if (r["source_url"], r["target_url"]) in labelled]
    good = sum(labelled[(r["source_url"], r["target_url"])] for r in judged)
    found = {(r["source_url"], r["target_url"]) for r in rows}
    return {"pairs": len(rows), "to_judge": to_ask, "accepted": len(accepted), "accepted_labelled": len(judged),
            "accepted_good": good, "precision": good / len(judged) if judged else None,
            "unlabelled_accepted": [(r["source_url"], r["target_url"], r["anchor"]) for r in accepted
                                    if (r["source_url"], r["target_url"]) not in labelled],
            "lost_good": [k for k, ok in labelled.items() if ok and k not in found]}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--score", dest="score_min", type=float)
    ap.add_argument("--context", dest="context_min", type=float)
    ap.add_argument("--anchor", dest="anchor_min", type=float)
    ap.add_argument("--per-target", dest="max_links_per_target", type=int)
    args = ap.parse_args(argv)
    cfg = load_config()
    cfg.update({k: v for k, v in vars(args).items() if k != "folders" and v is not None})
    for folder in args.folders:
        r = replay(Path(folder), cfg)
        p = f"{r['precision']:.0%}" if r["precision"] is not None else "-"
        print(f"{r['site']}: {r['pairs']} pairs ({r['to_judge']} not in the answer cache), "
              f"{r['labelled_pairs_found']}/{r['labelled_pairs']} labelled pairs still found, "
              f"{r['dropped_bad']} bad ones dropped; links {r['accepted']}, labelled {r['accepted_labelled']}, "
              f"good {r['accepted_good']} → precision {p}")
        for k in r["lost_good"]:
            print(f"  ! good pair no longer found: {k}")
        for s, t, a, ok in r["rephrased"]:
            print(f"  ~ new phrase for a pair labelled {'good' if ok else 'bad'}: {a!r} ({s} → {t})")
        m = replay_meaning(Path(folder), cfg)
        if m:
            p = f"{m['precision']:.0%}" if m["precision"] is not None else "-"
            print(f"  fan-out and semantic: {m['pairs']} pairs ({m['to_judge']} answers not in the cache), "
                  f"links {m['accepted']}, labelled {m['accepted_labelled']}, good {m['accepted_good']} → precision {p}")
            for k in m["lost_good"]:
                print(f"  ! good pair no longer judged: {k}")
            for k in m["unlabelled_accepted"]:
                print(f"  ? accepted, not labelled: {k}")


if __name__ == "__main__":
    sys.exit(main())
