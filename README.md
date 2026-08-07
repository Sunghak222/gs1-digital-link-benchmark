# GS1 Digital Link Multimodal RAG Benchmark

A benchmark for evaluating RAG pipelines that answer questions by following GS1 Digital Link resolvers.
Starting from open data (Open Food Facts and the Korea Tourism Organization's TourAPI), it builds a **fixture corpus of linksets, HTML pages, and images**, plus QA with gold answers.

The benchmark has two tracks:

- **v1.0 (frozen, `releases/v1.0/`)** — 20 hand-reviewed entities, 407 facts, **221 QA items**, and a counterfactual corpus. All evaluation runs use this release.
- **Expansion track (growing daily)** — an automated bulk pipeline extends the corpus in daily batches. As of 2026-07-27: **1,254 entities (1,000 food + 254 places), 31,935 facts, 9,685 pages, 3,988 images**. QA and counterfactuals for the new entities come later, at version milestones.

> 한국어판: [docs/README.ko.md](docs/README.ko.md) (v1.0 시점 기준)

Why build this from scratch? GS1 Digital Link is a standard that turns a barcode into a gateway to product resources — nutrition facts, certifications, images — via a "linkset". But very few services operate real linksets today. We had a pipeline to evaluate and no data to evaluate it on, so we built the benchmark starting from the data.

## Design highlights

**Facts are the single source of truth.** Source data is broken down into atomic facts (`facts.jsonl`), and everything else — HTML pages, QA gold answers, the counterfactual corpus — is derived from them. Since the values on the pages and the grading keys come from the same place, the answer key cannot disagree with the corpus by construction.

**A counterfactual corpus catches prior-knowledge cheating.** The entities are famous (Gyeongbokgung Palace, Heinz ketchup...), so an LLM can often answer without reading anything. We therefore built a parallel corpus in which 102 facts are deliberately altered. Ask the same question against both corpora: if the answers diverge, the model read the documents; if both come back with the real-world value, it answered from pretraining.

**Pages carry the original prose, not summaries.** If pages contained only the extracted essentials, the task would be too easy. So the paragraph a fact was buried in (`passage`) is rendered verbatim, and the value's character offsets (`value_span`) are recorded separately. Those offsets are also how the counterfactual build splices a fake value into exactly the right spot.

**Every run reproduces the same output.** API responses are cached as snapshots, extraction is deterministic rules, and the places an LLM is used (mining facts from prose, cross-checking label photos, drafting QA) all run at temperature 0 with disk caching. With a warm cache the entire corpus regenerates identically, offline.

**Quality gates are code, not judgment calls.** Whether an entity enters the corpus is decided by scripted gates — food: English data, a complete as-sold nutrition table, live photos; places: KOGL Type-1 licensed photos, a substantial overview, operating/contact information. Entities that fail after GLN assignment are retired permanently (`place_excluded`), never renumbered, so identifiers stay stable as the corpus grows.

## Dataset at a glance

| Item | v1.0 (frozen) | Expansion track (2026-07-22) |
|---|---|---|
| Entities | 20 — 10 food + 10 places | **694** — 440 food (real GTINs) + 254 Korean attractions (demo GLNs) |
| Facts | 407 | **31,935** |
| Pages | 130 per corpus | **9,685** per corpus — 4 rotating templates (plain / table-heavy / JSON-LD / noisy), incl. EUC-KR pages |
| Images | 58 | **2,308** — food front / nutrition label / ingredients + KOGL Type-1 place photos |
| QA | **221** — 210 html / 11 image, 126 EN / 95 KO, 5 multi-hop | pending (generated at version milestones) |
| Counterfactual | 102 altered facts | corpus mirrors all entities; alterations still v1.0's 102 (expansion pending) |

## Repository layout

Think of this repository as **an exam**. There is the exam paper the test-taker sees, and the answer key only the grader holds. Everything in here belongs to one side or the other.

**What the test-taker (the RAG pipeline) sees** — one folder per entity, shaped exactly like a real GS1 resolver would serve it:

```
entities/
└── 01-00000050457250/               # one entity = one folder (this one is Heinz ketchup;
    │                                #   "01/" = GTIN, "414/" = place GLN)
    ├── linkset.json                 # the entry point: "this product has a nutrition page,
    │                                #   an allergen page, 3 photos, ..." with links to each
    ├── pages/
    │   ├── pip.html                 # product info page (name, brand, categories)
    │   ├── nutritionalInfo.html     # the nutrition table
    │   ├── allergenInfo.html        # allergens + may-contain traces
    │   ├── ingredientsInfo.html     # ingredient list, additives, dietary analysis
    │   └── ...                      # 5–8 pages per entity
    └── media/
        ├── front.jpg                # product front photo
        ├── nutrition-label.jpg      # the label photo (what image-QA reads)
        └── ingredients.jpg
```

`counterfactual/` mirrors the same folders, except values inside the pages of the v1.0 entities are deliberately different. A model that actually reads the pages gives different answers there; a model answering from memory gives the same ones.

**What the grader holds** — the answer key, never shown to the pipeline:

| File | What it is | Handy version for humans |
|---|---|---|
| `facts.jsonl` | All 31,935 facts, one per line — the master record every page and answer was built from | `facts.pretty.json` (same content, grouped per entity → page for reading) |
| `qa.jsonl` | The 221 questions with gold answers and which facts prove them (v1.0 entities) | `qa.csv` (open in Excel) |
| `manifest.json` | Build record — most importantly the *placement map*: which page each fact was printed on. This is how the grader knows which page a retriever should have found | — |
| `releases/v1.0/` | The frozen v1.0 snapshot (entities, counterfactual, facts, QA, selection) — what experiments actually run on | — |

**How it was all made** — the machinery, needed only when regenerating or extending the dataset:

| Folder | Contents |
|---|---|
| `scripts/` | The construction pipeline: pick entities → snapshot sources → extract facts → render pages & linksets → validate → generate QA |
| `scripts/bulk/` + `scripts/bulk_expand.py` | The daily bulk-expansion pipeline (see "Growing the corpus" below) |
| `eval/` | The evaluation harness: runs the real RAG graph over the corpus, grades against gold (`run_eval.py`, `grade.py`, `report.py`) |
| `results/` | Timestamped evaluation runs |
| `work/` | Human decisions, script-to-script lists, review records — key files explained below |
| `schemas/` | JSON Schemas that every fact / QA / linkset must pass |
| `data/raw/` | Cached API responses and LLM calls (gitignored) — the reason re-runs are reproducible |
| `data/dump/` | The Open Food Facts full dump (~13 GB, gitignored) — the food candidate source |
| `docs/` | 01 design rationale / 02 implementation plan / 03 pipeline walkthrough / 04 evaluation-system design / 05 experiment budget / 06 findings / 07 OFF dump plan / 08 bulk-expansion plan |

### Inside `work/`

Every file is a human decision, a list one script makes for another, or a stage record.
The first five are **live** — their paths are wired into `scripts/`, so rename them only together with the scripts.

| File | What it is | Written by → read by |
|---|---|---|
| `selection.yaml` | ★ the chosen entities, appended batch by batch. **Order is identity**: place GLNs are numbered by position, so entries are append-only — never reordered or deleted. Failed places move to the `place_excluded` block (GLN permanently retired) | human + harvest scripts → extract_facts |
| `counterfactual-overrides.jsonl` | ★ human decision — which facts to alter; the sole input of the counterfactual corpus | human → build_fixtures, validate, gen_qa |
| `entity-map.json` | entity ID ↔ class · name · source id | extract_facts → build_fixtures, gen_qa, validate |
| `image-qa-exclusions.json` | (entity, nutrient) pairs where the label photo disagrees with the text — banned from image QA | extract_facts → gen_qa |
| `qa-ambiguous-facts.json` | facts whose answer token appears on two pages — banned as QA gold | validate → gen_qa |
| `expansion/day-NN/` + `day-NN-entities.txt` | per-batch records: the entity list, the review report (`entity-review.md`), QA drafts when generated | bulk pipeline ↔ human |
| `bulk/tour-pool.jsonl` / `tour-eval.jsonl` | the nationwide TourAPI sweep pool and the cumulative pass/fail verdicts (survives across days, so quota is never re-spent) | tour_harvest → tour_harvest |
| `qa-draft.jsonl` / `qa-review.csv` | QA drafts and the review sheet — finalized into `qa.jsonl` | gen_qa ↔ human |

## The pipeline, traced through one example

Here is Heinz Tomato Ketchup (GTIN 50457250) passing through each stage. The full story is in [docs/03-pipeline-walkthrough.md](docs/03-pipeline-walkthrough.md) (Korean).

**① Select entities** (`select_entities.py` for v1.0; `scripts/bulk/` now). Candidates are machine-scored for richness (photos, nutrients, allergens...), pass the quality gates, and land in `work/selection.yaml`:

```yaml
- "50457250"   # Heinz Tomato Ketchup — sauces / celery / GTIN-8 case
```

**② Snapshot the source** (`clients/`). Every later stage reads only this cache, so the benchmark stays frozen even if the source edits the product tomorrow.

**③ Break it into facts** (`extract_facts.py`). Not regex scraping — a flat list of mapping rules, each saying "this field becomes this predicate on this page". Heinz yields 25 facts:

```json
{"fact_id": "food-50457250-sugars_100g", "value": {"amount": 22.8, "unit": "g"},
 "linktype": "nutritionalInfo", "source": {"origin": "off", "field": "nutriments.sugars_100g"}}
```

An LLM is used only where structure runs out: the prose overviews of places. It must copy the phrase containing each fact verbatim, and the code re-locates that phrase in the original text — if it can't be found, the fact is dropped. The LLM points at values; it cannot invent them.

**④ Assemble pages, media, and the linkset** (`build_fixtures.py`). Reads facts only. Facts sharing a linktype are poured into a Jinja2 template, and the resulting pages are tied together as an RFC 9264 linkset:

```html
<tr><td>sugars</td><td>22.8 g</td><td>3.42 g</td></tr>
```

**⑤ Run the same builder once more with an override table** (`--overrides`). Heinz's sugars become 22.8 → 13.1 g (per-serving scaled proportionally), producing the counterfactual corpus:

```
original: <td>sugars</td><td>22.8 g</td><td>3.42 g</td>
CF      : <td>sugars</td><td>13.1 g</td><td>1.97 g</td>
```

**⑥ Validate, then generate QA** (`validate.py`, `gen_qa.py`). After seven integrity checks pass (every fact rendered, every answer on exactly one page, every CF override diverged, ...), an LLM drafts questions page by page — but **gold answers must be picked from that page's facts only**. For v1.0, the 230 drafts went through a full manual review, leaving 221:

```json
{"question": "What is the amount of sugars in 100g of Tomato Ketchup?",
 "gold_fact_ids": ["food-50457250-sugars_100g"],
 "gold_answer": "There are 22.8 g of sugars in 100g of Tomato Ketchup.",
 "tags": {"modality": "image", "lang": "en", "hop": "single"}}
```

This one QA condenses the whole pipeline: the gold points at a fact from ③, that fact's placement from ④ is the page a retriever should reach, and in the counterfactual corpus from ⑤ the same question must yield 13.1 g.

## Growing the corpus: the daily bulk loop

Since day-01 (2026-07-19) the corpus grows in daily batches (plan: [docs/08-bulk-expansion-plan.md](docs/08-bulk-expansion-plan.md)). One day's run:

```bash
python -m scripts.bulk_expand food  --target 200 --batch day-NN   # OFF dump → gate → select 200
python -m scripts.bulk_expand place --budget 950 --batch day-NN   # TourAPI eval within quota → select passers
python -m scripts.bulk_expand finalize --batch day-NN             # extract → build both corpora → validate → review report
```

- **Food uses zero API calls.** The OFF search API proved unreliable, so candidates come from the full dump: a CSV pre-filter (six English-speaking countries) followed by a final gate on the per-product JSONL snapshot.
- **Places are quota-aware.** A one-time nationwide sweep built a pool of 7,534 attractions; each day evaluates as many as the API budget allows (3 calls per place), and verdicts accumulate in `tour-eval.jsonl` so no call is ever repeated. Pass rate runs ≈ 37%.
- **Humans review reports, not raw data.** Each batch ends with `work/expansion/day-NN/entity-review.md` listing only exceptions: values that differ between photo and text, entities with few images, unverified items. Structural correctness (GLN order, schema, placement uniqueness) is asserted by code — `verify_batch.py` additionally proves that pre-existing pages are byte-identical after each batch.

Batch history: day-01–02 (pilot, 20+20+40) → day-03 (80→384) → day-04 (384→694).

## Reproducing

```bash
# Two keys in .env (or the parent kg_neo4j/.env):
#   TOUR_API_KEY=...      # Korea Tourism Organization open-data key
#   OPENAI_API_KEY=...    # gpt-4o-mini (prose mining, label cross-check, QA drafting)

python -m scripts.extract_facts            # build facts.jsonl
python -m scripts.build_fixtures           # build entities/
python -m scripts.build_fixtures --overrides work/counterfactual-overrides.jsonl   # build counterfactual/
python -m scripts.validate                 # 7 integrity checks
python -m scripts.gen_qa draft             # QA drafts + review CSV
python -m scripts.gen_qa finalize          # merge review verdicts → qa.jsonl
```

All API responses and LLM calls are cached under `data/raw/`, so with a warm cache the entire corpus regenerates identically, offline. To extend the corpus instead, use the bulk loop above; to run experiments, use `releases/v1.0/`.

## Evaluation harness

Implemented in `eval/` (design: [docs/04-evaluation-system-design.md](docs/04-evaluation-system-design.md)). It runs the **real RAG pipeline graph** — unmodified — against the v1.0 release, swapping only where documents come from. Two layers of output: **blackbox scores** graded purely on final answers against gold, and **trace diagnostics** parsed from logs (did the retriever reach the gold page, was the VLM invoked) reported as best-effort diagnostics, never as part of the score. Runs are compared across KG-hit / KG-miss states and original / counterfactual corpora; results land in `results/`.

## Sources and caveats

- Food data & photos: [Open Food Facts](https://openfoodfacts.org) (ODbL / CC-BY-SA), harvested from the full dump. It's crowdsourced, so some values differ from the real world — and we **deliberately did not correct them**: the benchmark's ground truth is this corpus, not reality (text-canonical policy). Nutrient pairs where label photos disagree with the text are excluded from image QA.
- Place data & photos: Korea Tourism Organization TourAPI. Only photos explicitly under the KOGL Type-1 license are adopted; entities without them are rejected at the gate.
- Place GLNs (`952...`) are fictional identifiers built on the GS1 demo prefix, and **every altered value in the counterfactual corpus is intentionally wrong.** Do not cite any number in this repository as real-world information.

## Limitations & TODO

- **QA covers only the 20 v1.0 entities** (221 items); QA for the 674 expansion entities is generated and reviewed at version milestones, not per batch.
- **Counterfactual alterations likewise cover only v1.0** (102 facts); the expansion entities' CF folders are currently unaltered mirrors.
- Image QA covers a single skill — reading nutrition labels — with 11 items. Front/ingredients photos and place photos are room to grow.
- Only 5 multi-hop questions; multi-hop QA is regenerated globally at milestones since answers shift as the corpus grows.
- Each batch currently rebuilds the full corpus; an incremental build mode is planned as the corpus grows.
- The food candidate pool is finite (~660 gate-passing products remain); widening the country filter or relaxing gates is an open decision.
