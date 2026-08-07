# 13. The Food (OFF) Extraction Funnel and What `finalize` Actually Does

*(English version of `13-추출-깔때기와-finalize.md`, measured 2026-08-05.)*

Related docs: 07 (dump processing), 08 (design & the 13 pitfalls), 12 (parallelization speedups), 14 (performance fixes). This document is a one-page summary of two things: **how 4.5M dump rows become the corpus**, and **exactly what the `finalize` step does**. Figures in §0–5 are a snapshot from the morning of 2026-08-05 (CAND_CAP 20,000); **final confirmed figures are in §6** (same day, evening — OFF fully exhausted).

## 0. The funnel at a glance

| Stage | Count | What gets filtered | Network |
|---|---|---|---|
| Full OFF CSV dump | 4,535,553 rows | — | none (local file) |
| ① scan: base filters | 93,815 (+11k already selected ≈ ~105k total) | the 5 filters in §1 | none |
| ② top-CAND_CAP by scan count | 20,000 | volume cap, not quality (popularity order) | none |
| ③ snapshot: English label | 16,150 (81%) | JSONL dump `lang` ≠ en | none |
| ④ select: gates + liveness | consumed 500 per round | the 4 gates in §3 + image liveness | image HEAD only |
| ⑤ finalize build gate | 1–8% dropped | fewer than 3 optional page sections | image downloads |

The food path uses **no APIs at all** (the search API died, so we moved to the full dump — docs/07). The only network traffic is the liveness probe in ④ and the image downloads in ⑤ — which is exactly why those two spots, and only those, were parallelized (docs/12).

## 1. ① scan — 4.5M CSV rows → ~105k (five base filters)

The CSV is read line by line (a few MB of memory). A row must pass all of:

1. **English-speaking market**: `countries_en` contains one of US / UK / Ireland / Australia / Canada / New Zealand (a market pre-filter — the *label language* verdict happens later, in ③)
2. **Nutrition label photo present**: `image_nutrition_url` exists
3. **Ingredients text present**: `ingredients_text` exists
4. **Valid barcode**: check-digit passes, and not already selected
5. **8+ nutrition values**: filled `*_100g` columns (excluding as-prepared) ≥ 8

## 2. ②③ The volume cap and the language verdict

- Survivors are sorted by **scan count** (`unique_scans_n` — how often consumers scanned the product with the OFF app) and only the top CAND_CAP are kept as candidates. Popular products tend to have richer data. When the pool runs dry, raise CAND_CAP and rescan (history: 5k → 10k → 20k → 40k → 80k).
- The 13 GB JSONL dump is swept once to pull each candidate's full record (36 snapshot fields). The `lang` field there gives the **final label-language verdict** — only `en` enters the shortlist. Measured English rate: ~80% in the top-5k band, still 81% at the 20k band, 78% at the deepest band — remarkably stable.

## 3. ④ select — 500 picks per round

The shortlist is walked in score order; a candidate must pass:

1. **7+ as-sold nutrition keys**: `_100g` keys in the snapshot's `nutriments`, excluding as-prepared
2. **Sane categories**: products tagged `en:null` are dropped
3. **Deduplication**: the normalized (brand + product name) key must not collide with anything already selected or picked earlier in the same round (same name under a *different* brand is allowed)
4. **Image liveness**: the nutrition-label photo must answer a HEAD request (mandatory). If the front or ingredients photo is dead, the *product survives* but **that photo is dropped** — these show up later in the review report as "fewer than 3 images"

Survivors are appended to `selection.yaml` and the batch roster (`day-NN-entities.txt`). At this point they are only *on the list* — not yet corpus.

## 4. ⑤ finalize — turning the list into the corpus

`python -m scripts.bulk_expand finalize --batch day-NN` runs, in order:

| Step | What it does | Measured (at 11k entities) |
|---|---|---|
| extract_facts | Snapshots → atomic facts (facts.jsonl) + entity map, regenerated for *all* classes every run (deterministic, idempotent) | 27 s after the fixes (was 7–10 min) |
| build_fixtures | **Gate** (required pages + at least 3 optional sections, or drop) → per entity: HTML pages on a 4-template rotation + linkset.json + image downloads (missing files only, prefetched 8-way) → manifest | pages ~1 min; images proportional to what's new; unchanged entities are reused via input fingerprints |
| build_fixtures --overrides | Same thing once more with counterfactual overrides, into the counterfactual/ tree (images are copied) | minutes |
| validate | 7 integrity checks: schemas, every fact's page exists and contains the value, gold uniqueness/ambiguity, counterfactual divergence, etc. | 27 s |
| verify_batch | Re-render-and-compare (same input → same output) + orphan-folder detection | ~20 s |
| make_review | Human-review report (`work/expansion/day-NN/entity-review.md`): value mismatches, thin images, licensing, category skew | seconds |

- Gate failures (GATE FAIL) and per-entity exceptions (BUILD FAIL) are listed at the end with exit code 1 — remove the casualties from the selection and re-run. The build isolates failures per entity and leaves no debris folders.
- Operating mode (agreed 2026-08-04): rounds run *select only*; build + verification happen as occasional checkpoints or one final pass.

## 5. Gate-failure history

| When | Dropped | Cause |
|---|---|---|
| day-07 (of 3,068) | 41 (1.3%) | all "only 2 optional sections" (thin content) |
| day-08 cp3 (of 3,000) | 97 (3.2%) | same — thinner products deeper in the pool |
| day-08 cp4 (of 2,000) | 133 (6.7%) | same |
| day-08 first full pass (of 10,894) | 497 (4.6%) | same |
| day-08 grand final (of 51,685) | 4,316 (8.4%) | same — the tail of the pool |
| **Total** | **5,084** | build exceptions (BUILD FAIL): **0 across the entire run** |

Casualty lists are preserved in `work/bulk/day-NN-gate-fail*.txt`.

## 6. Final results (evening of 2026-08-05 — OFF concluded)

With CAND_CAP raised 10k → 20k → 40k → 80k (three rescans) and **152 selection rounds** in total, every OFF product that passes the base filters has been consumed. The completed funnel:

| Stage | Count |
|---|---|
| Full OFF CSV dump | 4,535,553 |
| Passed the 5 base filters | **~105,500** |
| English label (78–81%, stable at every depth) | ~85,000 |
| Passed selection gates → selected | 80,240 |
| Passed the build gate = **final food corpus** | **75,197** |

**Confirmed corpus (all validate checks + verify_batch passing):**

- **75,471 entities** (food 75,197 / places 254 / OTC drugs 20)
- **1,880,481 facts** / 487,954 HTML pages / 211,516 images (plus one full counterfactual corpus)
- Flagged for human review: 14,637 entities with fewer than 3 images (dead front/ingredients photos — every entity still has its nutrition label), 8 facts with formatting residue, 175,018 ambiguous facts (auto-maintained QA-gold exclusion list)
- Measured finalize time at the 80k scale: **7 minutes** (after the fixes in docs/14; the pre-fix pipeline would have taken roughly 8 hours)

The food axis is hereby **concluded** — extracting more would require loosening the standards (e.g. accepting non-English labels), which we do not recommend. The remaining growth levers are places (TourAPI, batch day-09) and a bulk pharma expansion.




언어별, 채워져잇는 필드별 통계필요