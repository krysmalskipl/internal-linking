"""The suggestions on a real site, replayed without network against hand labels (tests/bench.py).

tests/benchmark/<site>/ holds a real run (pages and Jev answers) with every judged pair labelled by
hand, and expected.json with what the current code must keep: the precision of the links it picks
with the default thresholds, and the most pairs it may send to Jev. A change that loses a good pair,
lowers the precision or sends more pairs fails here. Sites that cannot be published can be checked
the same way: INTERNAL_LINKING_BENCHMARKS=/path/to/folder[:another] pytest tests/test_benchmark.py"""
import json
import os
from pathlib import Path

import pytest

import bench
from internal_linking.config import load_config

HERE = Path(__file__).parent / "benchmark"
FOLDERS = sorted(p for p in HERE.iterdir() if p.is_dir()) + [
    Path(p) for p in os.environ.get("INTERNAL_LINKING_BENCHMARKS", "").split(os.pathsep) if p]


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_suggestions_on_a_labelled_site(folder):
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8")) if (folder / "expected.json").exists() else {}
    result = bench.replay(folder, load_config())
    assert not result["lost_good"], f"good pairs no longer found: {result['lost_good']}"
    assert result["precision"] >= expected.get("min_precision", 0.9), result
    assert result["pairs"] <= expected.get("max_pairs", result["pairs"]), result


@pytest.mark.parametrize("folder", [f for f in FOLDERS if (f / "meaning_candidates.jsonl.gz").exists()], ids=lambda p: p.name)
def test_fanout_and_semantic_links_on_a_labelled_site(folder):
    """Fan-out and semantic pairs from a real run: every pair labelled good still reaches Jev with
    a link text, the picked links stay precise, none needs an answer that is not cached."""
    expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
    result = bench.replay_meaning(folder, load_config())
    assert not result["lost_good"], f"good pairs no longer judged: {result['lost_good']}"
    assert result["to_judge"] == 0, f"the phrases changed - {result['to_judge']} Jev answers missing: run the calibration again"
    assert result["precision"] is not None and result["precision"] >= expected["meaning_min_precision"], result
    assert result["accepted_good"] >= expected["meaning_min_good"], result
