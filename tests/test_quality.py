import json
import types

import numpy as np

from internal_linking.common import read_csv, set_data_root, write_csv
from internal_linking.quality import evaluate, sample

FIELDS = ["decision", "source_url", "source_title", "target_url", "target_title", "anchor", "anchor_type",
          "placement", "context", "in_menu", "score", "similarity", "jev_context", "jev_anchor", "jev_value",
          "jev_cannibalisation", "jev_verdict"]


def rec(i: int, good: bool, similarity: float | str, kind: str = "exact") -> dict:
    return {"decision": "accept", "source_url": f"https://example.pl/s{i}/", "source_title": f"S{i}",
            "target_url": f"https://example.pl/t{i}/", "target_title": f"T{i}", "anchor": f"fraza {i}",
            "anchor_type": kind, "placement": "paragraph", "context": f"Akapit numer {i} z frazą fraza {i}.",
            "in_menu": 0, "score": 0.96, "similarity": similarity, "jev_context": 0.9, "jev_anchor": 0.9,
            "jev_value": 0.75, "jev_cannibalisation": 0.1, "jev_verdict": "ok", "_good": good}


def setup(tmp_path, rows: list[dict], labelled: bool = True):
    set_data_root(tmp_path / "data")
    ddir = tmp_path / "data" / "example.pl"
    ddir.mkdir(parents=True, exist_ok=True)
    write_csv(ddir / "recommendations.csv", rows, FIELDS)
    if labelled:
        write_csv(ddir / "sample_to_label.csv", [{**r, "ok": int(r["_good"])} for r in rows],
                  ["ok", "source_url", "target_url"])
    return ddir


def test_sample_keeps_the_judged_passage_and_fills_it_in_for_old_samples(tmp_path):
    rows = [rec(i, True, 0.5, "exact" if i % 2 else "partial") for i in range(20)]
    ddir = setup(tmp_path, rows, labelled=False)
    # a sample drawn by 0.2.0: Polish column names, no passage
    write_csv(ddir / "sample_to_label.csv", [{"ok": "1", "kontekst": "", "source_url": rows[0]["source_url"],
                                             "target_url": rows[0]["target_url"]}], ["ok", "kontekst", "source_url", "target_url"])
    sample.sample("example.pl", n=6)
    out = read_csv(ddir / "sample_to_label.csv")
    assert len(out) == 7 and out[0]["ok"] == "1"
    assert all(r["context"].startswith("Akapit numer") and r["placement"] == "paragraph" for r in out)
    assert {r["anchor_type"] for r in out[1:]} == {"exact", "partial"}  # both match types drawn


def run_evaluate(tmp_path, rows) -> tuple[str, dict]:
    setup(tmp_path, rows)
    cfg_path = tmp_path / "cfg.json"
    evaluate.evaluate(types.SimpleNamespace(domain=["example.pl"], config=str(cfg_path), target_precision=0.9,
                                            score_floor=0.9, write_config=True))
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def test_similarity_threshold_is_set_when_it_separates_good_links(tmp_path, capsys):
    rng = np.random.default_rng(1)
    rows = [rec(i, True, round(float(rng.uniform(0.6, 0.9)), 3)) for i in range(30)]
    rows += [rec(30 + i, False, round(float(rng.uniform(0.2, 0.45)), 3)) for i in range(10)]
    cfg = run_evaluate(tmp_path, rows)
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("similarity"))
    assert "1.00" in line and line.split()[-1] == "40"  # AUC with an interval, on 40 pairs
    assert 0.3 < cfg["similarity_min"] < 0.65  # bad links (≤ 0.45) cut, good ones (≥ 0.6) kept


def test_no_similarity_threshold_when_it_does_not_help(tmp_path, capsys):
    rng = np.random.default_rng(2)
    rows = [rec(i, i % 5 != 0, round(float(rng.uniform(0.3, 0.9)), 3)) for i in range(40)]
    rows += [rec(40 + i, True, "") for i in range(5)]  # pairs without embeddings are fine too
    cfg = run_evaluate(tmp_path, rows)
    assert cfg["similarity_min"] == 0.0
    assert "similarity" in capsys.readouterr().out
