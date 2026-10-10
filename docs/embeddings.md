# Embeddings and the link graph

[← README](../README.md)

Jev judges one passage and one link at a time. Embeddings add the view from above: how close two pages are in meaning, and how the site hangs together.

## What is embedded

One vector per crawled page, from its title, H1, meta description and the start of its content - template blocks (menus, footers, repeated sidebars) are already removed by the crawler - cut to the first 8,000 characters (about 2,000 tokens), which is plenty to say what a page is about. Pages in every language are embedded; comparisons stay within one language version.

## Model

[`qwen/qwen3-embedding-8b`](https://openrouter.ai/qwen/qwen3-embedding-8b) (Qwen3 Embedding 8B: multilingual including Polish, 33K-token context, $0.01 per million tokens on OpenRouter at the time of writing) through OpenRouter's embeddings endpoint (`/api/v1/embeddings`). Its vectors have 4,096 dimensions; the tool asks for `dimensions: 768` - plenty to compare pages, and five times smaller to store and compare. It uses `OPENROUTER_API_KEY` - also when Jev runs on TypeSafe's own API. Change the model or the size in `.env` or the environment:

```bash
EMBEDDING_MODEL=qwen/qwen3-embedding-8b   # any embedding model on OpenRouter, e.g. qwen/qwen3-embedding-4b
EMBEDDING_DIMENSIONS=768
```

If a provider ignores `dimensions` and returns a longer vector, it is cut to the first 768 values and normalised again - Qwen embeddings are trained Matryoshka-style, so the start of a vector is itself a valid smaller embedding. Vectors are stored normalised (length 1), so the dot product of two pages is their cosine similarity.

## How it is sent and cached

20 texts per request, 4 requests in parallel. Every vector is saved to `data/<domain>/embeddings.jsonl` under a key made of the model, the dimensions and the embedded text, so a re-run sends only new or changed pages, and switching models re-embeds everything once. The cost of the first run is about (characters of the site's pages ÷ 4) tokens at the model's OpenRouter price - at most about 2,000 tokens per page, so a 1,000-page site is about 2 million tokens, $0.02 with Qwen3 Embedding 8B; after that, only changed pages cost anything. `--dry-run` shows how many pages would be embedded.

## What the vectors are used for

1. **`similarity` on every suggestion** - the cosine similarity of the source and the target page (0-100% in the report; the absolute values depend on the model and the site, so compare links with each other rather than with a fixed number). It is independent of the phrase: Jev checks that the passage fits the target, similarity checks that the two pages as a whole belong together. It is a column in both CSVs, a bar on every link in the report and a *Page similarity* slider.
2. **An optional threshold** - `similarity_min` in `config.json` (default `0`, off) rejects pairs of pages that are too far apart (`low_similarity`). Look at the histograms in the Graph view before you set it. Pairs without vectors (e.g. after a failed API call) are never rejected by it.
3. **The topic map** - t-SNE of the vectors (after PCA to 50 dimensions; PCA alone for sites under 12 pages, fixed seed, so the same pages give the same map). Pages close on the map are close in meaning, so a long line across the map is a link between distant topics, and a tight cluster with few lines is a topic that is not linked together.
4. **Closest pages without a link** - for every page, the 5 most similar pages in the same language version that it is not linked with in either direction today. They are link opportunities even where no matching phrase exists yet (e.g. for a new paragraph).

## The Graph view

Drawn with [force-graph](https://github.com/vasturiano/force-graph) 1.47.4 (MIT), inlined in the report, so it works offline.

- every crawled page is a circle: red = orphan (no links in from other pages' content today), amber = weak (1-2), grey = 3 or more; the size grows with the links in after the changes; the largest pages are labelled, the rest on hover or zoom
- lines: grey = links in content today, purple with an arrow = the new links currently selected by the filters on the left, dashed green = similar but not linked (off by default, always shown for the page in focus)
- *By topic* places pages on the topic map, *By links* lets linked pages pull together (link length from similarity); *Pages with new links* shows only the part of the site that changes (default above 1,500 pages)
- **click a page** (or find it in the search box, or use *graph ›* in the Pages view) and it zooms in: its pages - links today, new links and the closest unlinked ones - gather around it on rings of similarity (90 / 75 / 60 / 45%, the more similar, the closer) and the rest of the site moves aside; a side panel lists them grouped (new links out / in, links out / in today, closest without a link) with their similarity and the anchors of the new links; click any of them to move the focus there, Esc or a click on the background to go back
- below the graph: links today, new links, orphans today → after the changes, unlinked page pairs at least as similar as a typical link today, and histograms of the similarity of the links today and of the new links on one scale (with a table view)

Without embeddings (the API failed, the model is unavailable, or `--no-embeddings`) the run carries on: the report has no similarity, the graph uses *By links* and has no topic map or closest pages. Run `internal-linking embed --domain example.com` later to add them without crawling or judging again.

Page texts are sent to OpenRouter and the embedding model's provider, as passages already are for Jev.
