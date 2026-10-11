import json
import types
import zlib

import numpy as np
import pytest

from internal_linking import embeddings, pipeline
from internal_linking.common import read_csv, read_jsonl, set_data_root, write_jsonl
from internal_linking.config import load_config
from internal_linking.jev import pairs
from internal_linking.jev.client import JevError
from internal_linking.report import link_graph, write_report

from conftest import page


@pytest.fixture
def api(tmp_path, monkeypatch):
    """Embeddings API stand-in, no network: a bag-of-words vector per text, longer than asked for
    (like a provider that ignores `dimensions`), returned out of order. Records every request."""
    monkeypatch.chdir(tmp_path)  # no .env from the developer's working directory
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("EMBEDDING_DIMENSIONS", raising=False)
    calls = []

    def post(url, body, retries=4, timeout=60):
        calls.append(body)
        data = []
        for i, text in enumerate(body["input"]):
            v = np.zeros(1024)
            for w in text.lower().split():
                v[zlib.crc32(w.encode()) % 700] += 1
            data.append({"index": i, "embedding": v.tolist()})
        return {"data": data[::-1], "usage": {"cost": 0.001}}

    monkeypatch.setattr(embeddings, "post", post)
    return calls


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_vectors_are_cut_to_the_dimensions_and_normalised():
    v = embeddings.unit([3.0, 4.0, 12.0], 2)
    assert v.tolist() == pytest.approx([0.6, 0.8])


def test_pages_are_embedded_in_batches_of_20_and_cached(tmp_path, api):
    pages = [page(f"https://example.pl/p{i}/", f"Strona {i}", f"tekst o temacie {i}") for i in range(45)]
    vectors, cost = embeddings.embed_pages(pages, tmp_path)
    assert sorted(len(c["input"]) for c in api) == [5, 20, 20]  # batches go out in parallel, in any order
    assert {(c["model"], c["dimensions"]) for c in api} == {("qwen/qwen3-embedding-8b", 768)}
    assert len(vectors) == 45 and cost == pytest.approx(0.003)
    v = vectors["https://example.pl/p7/"]
    assert v.shape == (768,) and np.linalg.norm(v) == pytest.approx(1, abs=1e-3)
    # the answers came back out of order: each vector still belongs to its own page
    assert v @ vectors["https://example.pl/p8/"] < v @ v - 1e-3

    api.clear()
    pages[3]["text"] = "zupełnie nowa treść"
    again, cost = embeddings.embed_pages(pages, tmp_path)
    assert [c["input"] for c in api] == [[embeddings.page_text(pages[3])]]  # only the changed page
    assert len(again) == 45 and cost == pytest.approx(0.001)
    api.clear()
    cached, _ = embeddings.embed_pages(pages + [page("https://example.pl/nowa/", "Nowa", "t")], tmp_path,
                                       compute=False)
    assert not api and len(cached) == 45  # cached vectors only, no API calls
    assert embeddings.count_new(pages + [page("https://example.pl/nowa/", "Nowa", "t")], tmp_path) == 1


def test_model_and_dimensions_can_be_changed(tmp_path, api, monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "qwen/qwen3-embedding-4b")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "256")
    vectors, _ = embeddings.embed_pages([page("https://example.pl/", "Start", "tekst")], tmp_path)
    assert (api[0]["model"], api[0]["dimensions"]) == ("qwen/qwen3-embedding-4b", 256)
    assert vectors["https://example.pl/"].shape == (256,)


def test_similarity_goes_on_rows_and_can_reject_a_pair():
    vectors = {"/a": unit([1, 0]), "/b": unit([1, 1]), "/c": unit([0, 1])}
    rows = [{"source_url": "/a", "target_url": "/b"}, {"source_url": "/a", "target_url": "/c"},
            {"source_url": "/a", "target_url": "/unknown"}]
    embeddings.add_similarity(rows, vectors)
    assert [r.get("similarity") for r in rows] == [pytest.approx(0.707, abs=1e-3), 0.0, None]
    cfg = {"score_min": 0.9, "context_min": 0.5, "anchor_min": 0.5, "cannibalisation_max": 0.7, "similarity_min": 0.3}
    judged = {"score": 0.99, "jev_context": 0.9, "jev_anchor": 0.9, "jev_cannibalisation": 0.1, "jev_verdict": "ok"}
    assert pairs.reject_reason({**judged, **rows[0]}, cfg) == ""
    assert pairs.reject_reason({**judged, **rows[1]}, cfg) == "low_similarity"
    assert pairs.reject_reason({**judged, **rows[2]}, cfg) == ""  # no vectors: the threshold does not apply


def test_nearest_skips_linked_pages_and_other_languages():
    m = np.stack([unit([1, 0, 0]), unit([1, 0.1, 0]), unit([1, 0.2, 0]), unit([1, 0.05, 0]), unit([0, 0, 1])])
    found = embeddings.nearest(m, 2, skip=[{1}, {0}, set(), set(), set()], group=["pl", "pl", "pl", "en", "pl"])
    assert [j for j, _ in found[0]] == [2, 4]  # 1 is linked already, 3 is in another language version
    assert found[0][0][1] == pytest.approx(float(m[0] @ m[2]), abs=1e-3)


def test_topic_map_puts_similar_pages_together():
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=64), rng.normal(size=64)
    m = np.stack([unit(c + rng.normal(size=64) * 0.3) for c in [a] * 10 + [b] * 10])
    xy = embeddings.topic_map(m)
    assert xy.shape == (20, 2) and np.abs(xy).max() == pytest.approx(1)
    within = np.linalg.norm(xy[:10] - xy[:10].mean(0), axis=1).mean()
    between = np.linalg.norm(xy[:10].mean(0) - xy[10:].mean(0))
    assert between > 2 * within
    assert embeddings.topic_map(m[:4]).shape == (4, 2)  # small sites: PCA


def test_graph_and_report_carry_links_similarity_and_the_library(tmp_path):
    pages = [page("https://example.pl/a/", "Opony zimowe - Serwis", "t", content_links=["example.pl/b/"]),
             page("https://example.pl/b/", "Opony letnie - Serwis", "t", content_links=["example.pl/a/", "example.pl/x/"]),
             page("https://example.pl/c/", "Klimatyzacja - Serwis", "t")]
    vectors = {"https://example.pl/a/": unit([1, 0.2, 0]), "https://example.pl/b/": unit([1, 0.3, 0]),
               "https://example.pl/c/": unit([0.9, 0, 1])}
    g = link_graph(pages, vectors)
    assert [n["t"] for n in g["nodes"]] == ["Opony zimowe", "Opony letnie", "Klimatyzacja"]  # site suffix removed
    assert [lk[:2] for lk in g["links"]] == [[0, 1], [1, 0]]  # links to pages outside the crawl are left out
    assert g["links"][0][2] == pytest.approx(float(vectors["https://example.pl/a/"] @ vectors["https://example.pl/b/"]), abs=1e-3)
    assert [j for j, _ in g["near"][0]] == [2]  # b is linked already
    assert all("x" in n and "y" in n for n in g["nodes"])

    rows = [{"source_url": "https://example.pl/c/", "target_url": "https://example.pl/a/", "anchor": "opony zimowe",
             "score": "0.95", "similarity": ""}]
    out = tmp_path / "report.html"
    write_report(out, "example.pl", rows, load_config(), 3, pages=pages, vectors=vectors)
    html = out.read_text(encoding="utf-8")
    assert "__DATA__" not in html and "__GRAPH_LIB__" not in html and "ForceGraph" in html
    data = json.loads(html.split('<script type="application/json" id="data">')[1].split("</script>")[0])
    c_a = float(vectors["https://example.pl/c/"] @ vectors["https://example.pl/a/"])
    assert data["rows"][0]["si"] == pytest.approx(c_a, abs=1e-3)  # filled in from the vectors
    assert data["pages"]["https://example.pl/b/"] == {"title": "Opony letnie - Serwis", "in": 1, "any": 1, "out": 1}
    assert data["embeddings"].endswith("768 dimensions") and len(data["graph"]["nodes"]) == 3


def run_args(**kw):
    return types.SimpleNamespace(**{"no_crawl": True, "sitemap": None, "delay": 0, "sources_file": None,
                                    "targets_file": None, "keywords": None, "sources": None, "targets": None,
                                    "dry_run": False, "no_embeddings": False, "workers": 2, "max_links": None, **kw})


def fake_judge(state, lang="pl"):
    return {"answers": {"kontekst": {"noul": 0.9}, "anchor": {"noul": 0.9}, "wartosc": {"score": 3.0},
                        "kanibalizacja": {"noul": 0.1}, "powod": {"choice": "ok", "probabilities": {"ok": 0.97}}},
            "usage": {"cost": 0}}


def test_run_adds_similarity_and_the_graph_without_network(site, tmp_path, api, monkeypatch):
    set_data_root(tmp_path / "data")
    home = "Robimy też serwis klimatyzacji samochodowej i wymianę opon zimowych. " + site[0]["text"]
    site[0].update(text=home, blocks=[{"tag": "p", "text": home, "raw": home}])
    ddir = tmp_path / "data" / "example.pl"
    ddir.mkdir(parents=True)
    write_jsonl(ddir / "pages.jsonl", site)
    monkeypatch.setattr(pairs, "judge", fake_judge)

    result = pipeline.process("example.pl", run_args(), load_config())
    assert result["links"] > 0
    rows = read_csv(ddir / "recommendations.csv")
    assert rows and all(0 < float(r["similarity"]) <= 1 for r in rows)
    assert len(read_jsonl(ddir / "embeddings.jsonl")) == len(site)
    assert '"graph": {"nodes": [{"u": "https://example.pl/"' in (ddir / "report.html").read_text(encoding="utf-8")


def test_run_goes_on_without_embeddings_when_the_api_fails(site, tmp_path, api, monkeypatch, capsys):
    set_data_root(tmp_path / "data")
    ddir = tmp_path / "data" / "example.pl"
    ddir.mkdir(parents=True)
    write_jsonl(ddir / "pages.jsonl", site)
    monkeypatch.setattr(pairs, "judge", fake_judge)

    def broken(url, body, retries=4, timeout=60):
        raise JevError("API error 400 qwen/qwen3-embedding-8b is not a valid model ID")

    monkeypatch.setattr(embeddings, "post", broken)
    pipeline.process("example.pl", run_args(), load_config())
    assert "not a valid model ID" in capsys.readouterr().out
    assert all(r["similarity"] == "" for r in read_csv(ddir / "recommendations.csv"))
    assert (ddir / "report.html").exists()


def test_embed_command_embeds_saved_pages_and_rebuilds_the_report(site, tmp_path, api, monkeypatch):
    set_data_root(tmp_path / "data")
    ddir = tmp_path / "data" / "example.pl"
    ddir.mkdir(parents=True)
    write_jsonl(ddir / "pages.jsonl", site)
    monkeypatch.setattr(pairs, "judge", fake_judge)
    pipeline.process("example.pl", run_args(no_embeddings=True), load_config())
    assert not (ddir / "embeddings.jsonl").exists()

    embeddings.embed_command(types.SimpleNamespace(domain=["example.pl"], workers=2, max_links=None, config=None))
    assert len(read_jsonl(ddir / "embeddings.jsonl")) == len(site)
    html = (ddir / "report.html").read_text(encoding="utf-8")
    data = json.loads(html.split('<script type="application/json" id="data">')[1].split("</script>")[0])
    assert data["rows"] and all(r["si"] is not None for r in data["rows"])  # results saved before embeddings
    assert all("x" in n for n in data["graph"]["nodes"])


def test_report_script_is_valid_javascript(tmp_path):
    """A syntax error (e.g. a name declared twice) stops the whole report - check it with node."""
    import re
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = tmp_path / "report.html"
    write_report(out, "example.pl", [], load_config(), 3, pages=[page("https://example.pl/", "Start", "t")])
    scripts = re.findall(r"<script>(.*?)</script>", out.read_text(encoding="utf-8"), re.S)
    (tmp_path / "report.js").write_text(scripts[-1], encoding="utf-8")  # the report's own code (not the library)
    check = subprocess.run(["node", "--check", str(tmp_path / "report.js")], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
