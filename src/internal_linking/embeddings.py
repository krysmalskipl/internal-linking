"""Page embeddings: how close in meaning two pages are, for every link and for the link graph.

Every crawled page is turned into one vector - its title, H1, meta description and the start of
its content - by an embedding model on OpenRouter (qwen/qwen3.7-text-embedding, 768 dimensions;
EMBEDDING_MODEL / EMBEDDING_DIMENSIONS override). Vectors are L2-normalised, so the dot product of
two pages is their cosine similarity: about 1 for pages on the same subject, lower the further
apart they are. They are cached in data/<domain>/embeddings.jsonl, keyed by model, dimensions and
the embedded text, so re-runs only pay for new or changed pages.

The vectors give:
- `similarity` on every suggestion (source page ↔ target page), an optional threshold
  (`similarity_min` in config.json) and a filter in the report,
- the report's link graph: a topic map (t-SNE of the vectors - pages close on the map are close in
  meaning), the similarity of every link today and every new link, and per page the closest pages
  it is not linked with yet.
"""
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .common import Progress, append_jsonl, data_dir, read_jsonl
from .jev.client import JevError, api_key, load_dotenv, post

API_URL_DEFAULT = "https://openrouter.ai/api/v1/embeddings"
MODEL_DEFAULT = "qwen/qwen3.7-text-embedding"
DIMENSIONS_DEFAULT = 768
BATCH = 20         # texts per request - the per-request limit of Qwen embedding providers
MAX_CHARS = 8000   # title, H1, description and the start of the content (~2000 tokens) set the topic
NEAREST = 5        # closest pages per page kept for the report


def settings() -> tuple[str, int]:
    load_dotenv()
    return (os.environ.get("EMBEDDING_MODEL", MODEL_DEFAULT),
            int(os.environ.get("EMBEDDING_DIMENSIONS", DIMENSIONS_DEFAULT)))


def page_text(page: dict) -> str:
    """What is embedded for a page: what it is called and what it says, template blocks excluded."""
    parts = (page.get("title", ""), page.get("h1", ""), page.get("meta", ""), page.get("text", ""))
    return "\n".join(p for p in parts if p)[:MAX_CHARS]


def cache_key(text: str, model: str, dims: int) -> str:
    return hashlib.sha1(f"{model}\n{dims}\n{text}".encode()).hexdigest()


def unit(vector: list[float], dims: int) -> np.ndarray:
    """The first `dims` values, L2-normalised. Qwen embeddings are trained Matryoshka-style, so a
    prefix is itself a valid smaller embedding - this keeps the size right when a provider ignores
    the `dimensions` parameter."""
    v = np.asarray(vector[:dims], dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else v


def request(texts: list[str], model: str, dims: int) -> tuple[list[list[float]], float]:
    """One API call for up to BATCH texts → (vectors in input order, cost in USD)."""
    url = os.environ.get("EMBEDDING_API_URL", API_URL_DEFAULT)
    resp = post(url, {"model": model, "input": texts, "dimensions": dims})
    data = sorted(resp.get("data") or [], key=lambda d: d.get("index", 0))
    if len(data) != len(texts):
        raise JevError(f"embeddings: {len(data)} vectors for {len(texts)} texts - {str(resp)[:300]}")
    return [d["embedding"] for d in data], float((resp.get("usage") or {}).get("cost") or 0)


def embed_pages(pages: list[dict], ddir: Path, workers: int = 4,
                compute: bool = True) -> tuple[dict[str, np.ndarray], float]:
    """URL → unit vector for every page, and the cost of the new ones. Only pages not in the
    cache are sent; with compute=False nothing is sent and only cached vectors are returned."""
    model, dims = settings()
    cache_path = ddir / "embeddings.jsonl"
    cache = {r["key"]: r["vector"] for r in read_jsonl(cache_path)}
    keys, texts = {}, {}
    for p in pages:
        text = page_text(p)
        keys[p["url"]] = k = cache_key(text, model, dims)
        texts.setdefault(k, (text, p["url"]))
    todo = [k for k in texts if k not in cache] if compute else []
    cost = 0.0
    if todo:
        api_key()  # fail fast with a clear message when the key is missing
        print(f"embedding {len(todo)} pages with {model} ({dims} dimensions, {len(texts) - len(todo)} cached)...")
        batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
        progress = Progress("embedded", len(todo), "pages")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = ex.map(lambda batch: request([texts[k][0] for k in batch], model, dims), batches)
            for batch, (vectors, batch_cost) in zip(batches, results):
                for k, v in zip(batch, vectors):
                    cache[k] = [round(float(x), 5) for x in unit(v, dims)]
                    append_jsonl(cache_path, {"key": k, "url": texts[k][1], "model": model, "vector": cache[k]})
                cost += batch_cost
                progress.step(len(batch))
        print(f"embeddings: {len(todo)} new pages, {len(texts) - len(todo)} cached, cost ${cost:.4f}")
    return {url: unit(cache[k], dims) for url, k in keys.items() if k in cache}, cost


def count_new(pages: list[dict], ddir: Path) -> int:
    """Pages a run would send to the embeddings API (for --dry-run)."""
    model, dims = settings()
    cached = {r["key"] for r in read_jsonl(ddir / "embeddings.jsonl")}
    return len({cache_key(page_text(p), model, dims) for p in pages} - cached)


def similarity(vectors: dict[str, np.ndarray], a: str, b: str) -> float | None:
    va, vb = vectors.get(a), vectors.get(b)
    return None if va is None or vb is None else round(float(va @ vb), 3)


def add_similarity(rows: list[dict], vectors: dict[str, np.ndarray]) -> None:
    """`similarity` (cosine of the source and target pages) on every row both pages have a vector for."""
    for r in rows:
        s = similarity(vectors, r["source_url"], r["target_url"])
        if s is not None:
            r["similarity"] = s


def topic_map(m: np.ndarray, seed: int = 0) -> np.ndarray:
    """2D positions in [-1, 1] where pages close in meaning sit close together: t-SNE (on the
    first 50 principal components), PCA for very small sites."""
    from sklearn.decomposition import PCA
    from sklearn.manifold import TSNE

    n = len(m)
    if n < 3:
        return np.array([[-0.5, 0.0], [0.5, 0.0]][:n])
    if n < 12:  # t-SNE needs more points than its perplexity
        xy = PCA(2, random_state=seed).fit_transform(m)
    else:
        x = PCA(min(50, n - 1, m.shape[1]), random_state=seed).fit_transform(m)
        xy = TSNE(2, perplexity=min(30.0, (n - 1) / 3), init="pca", random_state=seed).fit_transform(x)
    xy = xy - xy.mean(axis=0)
    return xy / (np.abs(xy).max() or 1)


def nearest(m: np.ndarray, k: int, skip: list[set[int]], group: list[str]) -> list[list[tuple[int, float]]]:
    """For every page, the k most similar pages in the same group (language version) that are not
    in skip[i] (pages it is already linked with): [(index, similarity), ...], best first."""
    n, out = len(m), []
    groups = np.asarray(group)
    for start in range(0, n, 512):  # in blocks, so large sites never hold an n × n matrix
        sims = m[start:start + 512] @ m.T
        for r, row in enumerate(sims):
            i = start + r
            row[i] = -np.inf
            row[np.fromiter(skip[i], dtype=int, count=len(skip[i]))] = -np.inf
            row[groups != groups[i]] = -np.inf
            top = np.argpartition(-row, k)[:k] if n > k else np.arange(n)
            top = sorted(top, key=lambda j: -row[j])
            out.append([(int(j), round(float(row[j]), 3)) for j in top if np.isfinite(row[j])])
    return out


def embed_command(args) -> None:
    """`embed`: vectors for the saved pages, then the report rebuilt - no crawl, no Jev calls."""
    from . import report  # report uses this module for the graph

    for domain in args.domain:
        ddir = data_dir(domain)
        pages = read_jsonl(ddir / "pages.jsonl")
        if not pages:
            print(f"! {domain}: no pages.jsonl - run `internal-linking run --domain {domain}` first")
            continue
        vectors, _ = embed_pages(pages, ddir, args.workers)
        print(f"{domain}: {len(vectors)} of {len(pages)} pages have a vector → {ddir / 'embeddings.jsonl'}")
    report.rebuild(args)


def add_parser(sub) -> None:
    p = sub.add_parser("embed", help="embed the saved pages (similarity, link graph) and rebuild the report")
    p.add_argument("--domain", action="append", required=True, help="domain (repeatable)")
    p.add_argument("--max-links", type=int, help="default links-per-page filter in the report")
    p.add_argument("--workers", type=int, default=4, help="parallel embedding requests")
    p.set_defaults(func=embed_command)
