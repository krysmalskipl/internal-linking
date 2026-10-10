# internal-linking

Internal link suggestions for any website, in one command. The tool finds phrases that **already exist** in your content (exact or partial match of another page's name) and asks [Jev](https://openrouter.ai/) - a typed-decision model by TypeSafe, available through OpenRouter - to judge every page → phrase → target pair. Every page is also turned into an embedding (Qwen3 Embedding 8B, 768 dimensions, also through OpenRouter), so each link gets a similarity score and the report can draw the whole site as an interactive graph. The output is a CMS-agnostic HTML report and CSV with links ready to insert.

> Works with Polish and English pages. The language is detected per page (`<html lang>`, or the text when the attribute is missing), so bilingual sites get links within each language version. Other languages are skipped.

![Interactive report: filters in % on the left, each link shown as where (the paragraph with the highlighted phrase), what (the link text) and where to (the target page)](examples/report.png)

Full example: [`examples/report-krysmalski.pl.html`](examples/report-krysmalski.pl.html) - a real run on a 43-page SEO blog: 28 links on 20 pages, 140 suggestions judged, $0.01. Download it and open it in a browser. (It was made with 0.1.0, so it has no Graph view or similarity yet - see [Embeddings and the link graph](#embeddings-and-the-link-graph).)

## Why?

- **Links on text that is already there.** Most internal linking tools suggest "related pages" and leave it to you to find a spot, or write new sentences for you. This one only proposes a link where a matching phrase already exists, so you get the paragraph, the phrase and the target - nothing to rewrite.
- **Judged, not guessed.** Jev answers closed questions with probabilities (is the passage on topic, is the phrase a natural anchor, would the link cannibalise the source page), so decisions come from thresholds you can measure and tune instead of from reading generated prose.
- **Cheap enough for every site.** One Jev call per pair, cached between runs - a 50-page site costs about a cent.
- **Safe defaults.** No links from headings, no duplicates of existing links, no phrases repeated like a template, same language only, at most 3 new links per page.
- **See the whole structure.** Page embeddings show how close two linked pages are in meaning, which pages are close but not linked, and how the site is linked today versus after the changes - as a graph you can click through.

The default thresholds were calibrated on Polish service and blog sites; on a new kind of site (e.g. an online store) or on English pages, check a sample first (see [Thresholds and quality checks](#thresholds-and-quality-checks-optional)).

## Install

```bash
git clone https://github.com/krysmalskipl/internal-linking && cd internal-linking
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env   # put your OpenRouter API key in .env
```

One OpenRouter key covers both Jev and the embeddings.

On Windows (PowerShell):

```powershell
git clone https://github.com/krysmalskipl/internal-linking; cd internal-linking
py -m venv .venv; .venv\Scripts\Activate.ps1; pip install -e .
copy .env.example .env   # put your OpenRouter API key in .env
```

Open a report with `open data/example.com/report.html` (macOS) or `start data\example.com\report.html` (Windows).

## Usage

```bash
internal-linking run --domain example.com
internal-linking run --domain a.com --domain b.com       # several domains
internal-linking run --domains-file domains.txt          # one domain per line
internal-linking run --domain example.com --dry-run      # count pairs and estimate cost, no Jev calls
internal-linking run --domain example.com --no-crawl     # reuse downloaded pages
internal-linking run --domain example.com --max-links 5  # new links per page (default 3)
internal-linking run --domain shop.com --targets '/category/|/blog/'   # large shop: link only to categories and posts
internal-linking run --domain shop.com --sources '^/blog/'              # only blog posts get new links (to any page)
internal-linking run --domain example.com --sources-file urls.txt        # only these pages get new links (URLs or paths, one per line)
internal-linking run --domain example.com --sources '^/blog/' --keywords services.csv   # blog → service pages on your phrases
internal-linking run --domain example.com --keywords keywords.csv   # keyword mode, see below
internal-linking run --domain example.com --no-embeddings   # skip page embeddings (no similarity, no topic map)
internal-linking embed --domain example.com              # embed the saved pages and rebuild the report - no crawl, no Jev calls
internal-linking report --domain example.com             # rebuild report.html from saved results - no crawl, no API calls
internal-linking questions --lang en                     # print the questions sent to Jev (en / pl)
```

(With the virtualenv not activated, use `.venv/bin/internal-linking`.)

Results land in `data/<domain>/` (change with `--data-dir`):
- `report.html` - an interactive, self-contained report: every link as *where* (the paragraph with the highlighted phrase), *what* (the link text) and *where to* (the target), with copy buttons for the text, the URL and ready HTML; sliders in % for every Jev score, page similarity, links per page, match type and search change the selection live; views: *Linking to* (the new links each page will get), *Linked from* (where each target's new links come from, with its links today - orphans and weakly linked pages flagged), *Pages* (every page with links in and out today, new links in and out, sortable by the weakest linked), *Graph* (the whole site as an interactive graph, see below), *Keywords* and *Rejected* (with reasons); collapsible groups; light / dark switch; CSV export of the current selection
- `links.csv` - links to insert
- `recommendations.csv` - every judged suggestion with its decision, reason and page similarity
- `embeddings.jsonl` - one vector per page (cache)

Jev answers (`jev_pairs.jsonl`) and page embeddings (`embeddings.jsonl`) are cached, so re-runs only pay for new or changed pairs and pages. Rough numbers: a 50-page site takes about a minute and about $0.01; downloading is the slow part on large sites (about 0.5 s per page), the phrase search takes seconds thanks to a word index. `--sources` / `--targets` (regexes on the URL path) and `--sources-file` / `--targets-file` (lists of URLs or paths) choose which pages get links and which pages links may point to - the whole site is still crawled, so e.g. blog posts can link to service pages. On a shop with thousands of products they also keep the number of Jev calls - and the cost - under control; check it first with `--dry-run`.

## Keyword mode

By default the tool works out targets on its own: every page's title, H1 and slug are its keywords. With `--keywords` it links **only the phrases you list** - a keyword → URL map like the ones used in SEO audits:

```csv
keyword,target_url,match
bathroom renovation,https://example.com/bathroom-renovation/,exact
kitchen renovation,,partial
```

- `target_url` is optional - without it the target is the page whose title, H1 or slug matches the keyword best (marked "auto" in the report)
- `match`: `exact` (the whole keyword, inflection-aware) or `partial` (at least 2 of its words; default); one-word keywords are allowed here
- a plain text file with one keyword per line works too
- the same safety rules and the Jev check apply; each keyword is linked at most once per page, with no site-wide limit
- `keywords_summary.csv` and the table at the top of the report show, per keyword, where it was found, how many links it got and why the rest were rejected

## Embeddings and the link graph

Jev judges one passage and one link at a time. Embeddings add the view from above: how close two pages are in meaning, and how the site hangs together.

**What is embedded.** One vector per crawled page, from its title, H1, meta description and the start of its content - template blocks (menus, footers, repeated sidebars) are already removed by the crawler - cut to the first 8,000 characters (about 2,000 tokens), which is plenty to say what a page is about. Pages in every language are embedded; comparisons stay within one language version.

**Model.** [`qwen/qwen3-embedding-8b`](https://openrouter.ai/qwen/qwen3-embedding-8b) (Qwen3 Embedding 8B: multilingual including Polish, 33K-token context, $0.01 per million tokens on OpenRouter at the time of writing) through OpenRouter's embeddings endpoint (`/api/v1/embeddings`). Its vectors have 4,096 dimensions; the tool asks for `dimensions: 768` - plenty to compare pages, and five times smaller to store and compare. It uses the same `OPENROUTER_API_KEY` as Jev. Change the model or the size in `.env` or the environment:

```bash
EMBEDDING_MODEL=qwen/qwen3-embedding-8b   # any embedding model on OpenRouter, e.g. qwen/qwen3-embedding-4b
EMBEDDING_DIMENSIONS=768
```

If a provider ignores `dimensions` and returns a longer vector, it is cut to the first 768 values and normalised again - Qwen embeddings are trained Matryoshka-style, so the start of a vector is itself a valid smaller embedding. Vectors are stored normalised (length 1), so the dot product of two pages is their cosine similarity.

**How it is sent and cached.** 20 texts per request, 4 requests in parallel. Every vector is saved to `data/<domain>/embeddings.jsonl` under a key made of the model, the dimensions and the embedded text, so a re-run sends only new or changed pages, and switching models re-embeds everything once. The cost of the first run is about (characters of the site's pages ÷ 4) tokens at the model's OpenRouter price - at most about 2,000 tokens per page, so a 1,000-page site is about 2 million tokens, $0.02 with Qwen3 Embedding 8B; after that, only changed pages cost anything. `--dry-run` shows how many pages would be embedded.

**What the vectors are used for:**

1. **`similarity` on every suggestion** - the cosine similarity of the source and the target page (0-100% in the report; the absolute values depend on the model and the site, so compare links with each other rather than with a fixed number). It is independent of the phrase: Jev checks that the passage fits the target, similarity checks that the two pages as a whole belong together. It is a column in both CSVs, a bar on every link in the report and a *Page similarity* slider.
2. **An optional threshold** - `similarity_min` in `config.json` (default `0`, off) rejects pairs of pages that are too far apart (`low_similarity`). Look at the histograms in the Graph view before you set it. Pairs without vectors (e.g. after a failed API call) are never rejected by it.
3. **The topic map** - t-SNE of the vectors (after PCA to 50 dimensions; PCA alone for sites under 12 pages, fixed seed, so the same pages give the same map). Pages close on the map are close in meaning, so a long line across the map is a link between distant topics, and a tight cluster with few lines is a topic that is not linked together.
4. **Closest pages without a link** - for every page, the 5 most similar pages in the same language version that it is not linked with in either direction today. They are link opportunities even where no matching phrase exists yet (e.g. for a new paragraph).

**The Graph view** (drawn with [force-graph](https://github.com/vasturiano/force-graph) 1.47.4, MIT, inlined in the report, so it works offline):

- every crawled page is a circle: red = orphan (no links in from other pages' content today), amber = weak (1-2), grey = 3 or more; the size grows with the links in after the changes; the largest pages are labelled, the rest on hover or zoom
- lines: grey = links in content today, purple with an arrow = the new links currently selected by the filters on the left, dashed green = similar but not linked (off by default, always shown for the page in focus)
- *By topic* places pages on the topic map, *By links* lets linked pages pull together (link length from similarity); *Pages with new links* shows only the part of the site that changes (default above 1,500 pages)
- **click a page** (or find it in the search box, or use *graph ›* in the Pages view) and it zooms in: its pages - links today, new links and the closest unlinked ones - gather around it on rings of similarity (90 / 75 / 60 / 45%, the more similar, the closer) and the rest of the site moves aside; a side panel lists them grouped (new links out / in, links out / in today, closest without a link) with their similarity and the anchors of the new links; click any of them to move the focus there, Esc or a click on the background to go back
- below the graph: links today, new links, orphans today → after the changes, unlinked page pairs at least as similar as a typical link today, and histograms of the similarity of the links today and of the new links on one scale (with a table view)

Without embeddings (the API failed, the model is unavailable, or `--no-embeddings`) the run carries on: the report has no similarity, the graph uses *By links* and has no topic map or closest pages. Run `internal-linking embed --domain example.com` later to add them without crawling or judging again.

Page texts are sent to OpenRouter and the embedding model's provider, as they already are for Jev.

## How it works

1. **Crawl** (`crawl.py`) - respects robots.txt (`Disallow`, `Crawl-delay`), reads the sitemap (from `robots.txt` or common locations) and, for each page, the title, H1, meta description, language, typed content blocks (paragraph, list item, heading...) and existing links (in content, breadcrumbs, elsewhere). Skips redirects, `noindex` and pages canonicalised elsewhere. Template blocks - text repeated verbatim across pages of the same language (top bars, sidebars, "areas we cover" sections, author bios) - are then removed from the content, so any theme works without configuration.
2. **Phrases** (`anchors.py`) - searches paragraphs and list items (never headings) for phrases matching another page's title, H1 or slug, inflection-aware, with per-language lists of function words and title fillers:
   - `exact` - the phrase covers the target's full name,
   - `partial` - at least two words of the target's name, including a distinctive one,
   - existing link text and code are never used; no single words or generic fillers.
3. **Rules** (`pipeline.py`) - no targets the page already links to (template links - menu, footer, sidebars, counted per language version - don't count), no target whose title or H1 is shared by several pages (e.g. the brand name as H1), same language only, no phrases repeated like a template (e.g. an author byline), a phrase that is the exact name of another page is reserved for that page.
4. **Embeddings** (`embeddings.py`) - one vector per page (see above), cached; the similarity of the two pages goes on every pair.
5. **Jev** (`jev/pairs.py`) - one call per pair, with the source page, the section heading, the passage and its neighbouring passages, and the target page; closed questions: does the paragraph discuss the target's topic, is the phrase a natural anchor, link value (0-4), cannibalisation, overall verdict with a reason code. Inspired by the Jev mode in [newsjack](https://github.com/elvisun/newsjack). Print the questions with `internal-linking questions`.
6. **Selection** - thresholds from `config.json` (shared by all domains), one link per phrase, at most N links per page by score.
7. **Report** (`report.py`) - the HTML report with the graph data (pages, links today, topic map, closest pages) built in.

## Thresholds and quality checks (optional)

The default thresholds (`src/internal_linking/config.json`) were set on hand-labelled samples. A `config.json` in the working directory, or `--config`, overrides them. To check or retune them on your own sites:

```bash
internal-linking sample --domain example.com      # random sample of suggestions
internal-linking label --domain example.com       # label Yes / No in the browser (localhost:8765)
internal-linking evaluate --domain example.com --write-config   # signal AUC and new thresholds → ./config.json
```

## Project layout

```
src/internal_linking/
├── cli.py            # `internal-linking` command
├── pipeline.py       # candidates (automatic and keyword mode), rules, selection
├── keywords.py       # keyword list loading, target auto-pick
├── crawl.py          # sitemap, robots.txt, content blocks, existing links
├── anchors.py        # exact / partial phrase matching, language detection
├── embeddings.py     # page embeddings (Qwen3 Embedding 8B via OpenRouter), similarity, topic map, `embed` command
├── report.py         # interactive report.html with the Graph view, `report` command
├── vendor/           # force-graph (MIT), inlined into the report
├── config.py         # thresholds (config.json)
├── jev/client.py     # OpenRouter API: Jev decisions, POST with retries
├── jev/pairs.py      # questions per pair, answer → columns, hard rules
└── quality/          # sample, label, evaluate
tests/                # pytest, no network: pip install -e ".[dev]" && pytest
```

## Notes

- The API client is `jev/client.py` (model `typesafe/jev-1.13`; `JEV_MODEL` / `JEV_API_URL` override the defaults). Jev is served from OpenRouter's alpha decisions endpoint, which may change.
- Embeddings use `qwen/qwen3-embedding-8b` by default (`EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, `EMBEDDING_API_URL` override it). If the embeddings API fails (e.g. a model that is no longer listed), the run prints the API error and carries on without embeddings - fix `.env` and run `internal-linking embed`.
- The crawler identifies itself as `internal-linking`, respects robots.txt and waits 0.5 s between requests by default (`--delay`). Only run it on sites you own or have permission to analyse.

## Changelog

The package is public, so every release that changes the output or the command line is listed here. Versions follow [semantic versioning](https://semver.org/); before 1.0, a minor version may change the output files.

### 0.2.0 - 2026-10-10

Added
- Page embeddings (`embeddings.py`): one vector per page with `qwen/qwen3-embedding-8b` through OpenRouter, 768 dimensions, cached in `embeddings.jsonl`; `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` and `EMBEDDING_API_URL` to change them.
- `similarity` (source page ↔ target page) on every suggestion: a column in `recommendations.csv` and `links.csv`, a bar on every link and a *Page similarity* slider in the report, and an optional `similarity_min` threshold in `config.json` (default 0, off) with the reject reason `low_similarity`.
- *Graph* view in the report: the whole site with links today and the new links, a topic map from the embeddings or a link-based layout, click-to-focus with similarity rings and a side panel, closest unlinked pages, similarity histograms and summary tiles. force-graph 1.47.4 (MIT) is inlined, so the report still works offline.
- `internal-linking embed --domain ...`: embeds the saved pages and rebuilds the report, without crawling or calling Jev.
- `run --no-embeddings` to skip the embeddings; `--dry-run` also counts the pages to embed.
- *Pages* view: a *Links out today* column and a *graph ›* shortcut.
- `numpy` is now a direct dependency (it already came with scikit-learn).

Changed
- The run continues without similarity and the topic map when the embeddings API fails, and says how to add them later.
- `report` loads cached embeddings and fills in the similarity of results saved before embeddings existed.
- The report's CSV export has a `similarity` column.
- `recommendations.csv` and `links.csv` have a new `similarity` column after `score`; scripts that read columns by position need updating.

### 0.1.0 - 2026-09-29

First public version.
- Crawl from the sitemap with robots.txt support, typed content blocks and template-block removal that works on any theme.
- Exact and partial phrase matching (inflection-aware) for Polish and English, language detected per page.
- One Jev call per pair with closed questions (context, anchor, value, cannibalisation, verdict), cached; retries on 429, 5xx and Cloudflare 52x errors; 12 parallel calls by default.
- Keyword mode (`--keywords`), page filters (`--sources`, `--targets`, `--sources-file`, `--targets-file`) and a fast word index for large sites.
- Interactive report (Linking to, Linked from, Pages, Keywords, Rejected; light / dark theme) and the `report` command; quality tools `sample`, `label`, `evaluate`.

## License

MIT
