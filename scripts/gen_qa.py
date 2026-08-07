"""S9 — QA generation: LLM drafts per page + programmatic multi-hop, human review via CSV.

    python -m scripts.gen_qa draft      # -> work/qa-draft.jsonl + work/qa-review.csv
    python -m scripts.gen_qa finalize   # review verdicts merged -> qa.jsonl

Rules enforced here (design doc 01 §S9):
  * gold candidates exclude work/qa-ambiguous-facts.json (evidence not unique).
  * modality=image only for nutrition facts whose label photo AGREES with the text
    (work/image-qa-exclusions.json lists the disagreeing pairs).
  * multi-hop questions come only from relation facts that exist in facts.jsonl
    (same brand / same region); answers are computed from fact values, not written
    by the LLM. Facts under CF override are avoided as multi-hop gold so the
    membership answer stays identical across both corpora.
  * language follows the document (food=en, place=ko); every 7th page flips the
    question language and is tagged cross_lingual (default policy, doc 02 §4).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from jsonschema.validators import validator_for

from scripts.common.config import BENCH_ROOT, SCHEMA_DIR, WORK_DIR
from scripts.common.llm import llm_json
from scripts.domains import get as get_domain

QA_SCHEMA = json.loads((SCHEMA_DIR / "qa.schema.json").read_text(encoding="utf-8"))
_QA_VALIDATOR = validator_for(QA_SCHEMA)(QA_SCHEMA)  # compiled once, not per call

_MANIFEST: dict | None = None


def _manifest() -> dict:
    """Load the manifest once; it used to be parsed in full twice."""
    global _MANIFEST
    if _MANIFEST is None:
        _MANIFEST = json.loads((BENCH_ROOT / "manifest.json").read_text(encoding="utf-8"))
    return _MANIFEST
DRAFT_PATH = WORK_DIR / "qa-draft.jsonl"
REVIEW_CSV = WORK_DIR / "qa-review.csv"
FINAL_PATH = BENCH_ROOT / "qa.jsonl"

QA_MODEL = "gpt-4o-mini"
PER_PAGE = 2          # LLM questions per page (pip/nutrition get 3)
IMAGE_SHARE = 0.25    # modality target; video is 0 in v1 (no video media) — warned

CSV_COLUMNS = ["qa_id", "entity_name", "linktype", "modality", "lang", "hop", "difficulty",
               "question", "gold_answer", "gold_fact_ids", "evidence_page",
               "verdict", "edited_question", "edited_answer"]


def _load() -> tuple[list[dict], dict, dict, set[str], set[tuple[str, str]], set[str]]:
    facts = [json.loads(l) for l in (BENCH_ROOT / "facts.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    entity_map = json.loads((WORK_DIR / "entity-map.json").read_text(encoding="utf-8"))
    placement = _manifest()["placement"]
    ambiguous = {a["fact_id"] for a in json.loads((WORK_DIR / "qa-ambiguous-facts.json").read_text(encoding="utf-8"))}
    img_excl = {(x["source_id"], x["predicate"].rsplit("_", 1)[0])   # nutrient base name
                for x in json.loads((WORK_DIR / "image-qa-exclusions.json").read_text(encoding="utf-8"))}
    overridden = {json.loads(l)["fact_id"]
                  for l in (WORK_DIR / "counterfactual-overrides.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    return facts, entity_map, placement, ambiguous, img_excl, overridden


# ---------------------------------------------------------------------------
# single-hop drafts (LLM, per page)
# ---------------------------------------------------------------------------


def draft_page(entity: str, name: str, cls: str, linktype: str,
               page_facts: list[dict], q_lang: str) -> list[dict]:
    n = 3 if linktype in ("pip", "nutritionalInfo") else PER_PAGE
    lang_word = {"ko": "한국어", "en": "English"}[q_lang]
    fact_lines = []
    for f in page_facts:
        v = json.dumps(f["value"], ensure_ascii=False)
        fact_lines.append(f"- {f['fact_id']}: {f['predicate']} = {v}")
    prompt = (
        f"벤치마크 QA 초안 작성. 대상: {name} ({cls}), 페이지: {linktype}.\n"
        f"아래 팩트만 골드 근거로 사용할 수 있다:\n" + "\n".join(fact_lines) + "\n\n"
        f"이 페이지에서 답할 수 있는 자연스러운 사용자 질문을 최대 {n}개 작성하라. 규칙:\n"
        f"1. 질문은 {lang_word}로. 답은 질문과 같은 언어로 간결하게.\n"
        "2. gold_fact_ids에는 위 목록의 fact_id만, 답의 근거가 되는 것만 넣는다.\n"
        "3. 값이 위 팩트에 없으면 그 질문은 만들지 않는다. 상식으로 답을 지어내지 마라.\n"
        "4. difficulty: 값 하나를 바로 찾으면 easy, 비교·계산·조건 해석이 필요하면 hard.\n"
        'JSON: {"questions": [{"question": ..., "gold_fact_ids": [...], "gold_answer": ..., "difficulty": "easy|hard"}]}'
    )
    # cache key must move when fact VALUES change, not just the count — otherwise a
    # value fix (e.g. the 2026-07-21 <br> cleanup) silently serves stale drafts
    vals = hashlib.sha256(json.dumps([[f["fact_id"], f["value"]] for f in page_facts],
                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
    key = hashlib.sha256(f"qa|{QA_MODEL}|{entity}|{linktype}|{q_lang}|{vals}".encode()).hexdigest()[:24]
    data = llm_json(key, [{"role": "user", "content": prompt}], QA_MODEL)
    return data.get("questions", [])[:n]


# ---------------------------------------------------------------------------
# multi-hop (programmatic — relation facts only, computed answers)
# ---------------------------------------------------------------------------


def multihop(facts: list[dict], entity_map: dict, overridden: set[str]) -> list[dict]:
    by_entity: dict[str, dict[str, dict]] = defaultdict(dict)
    for f in facts:
        by_entity[f["entity"]][f["predicate"]] = f
    name = {e: m["name"] for e, m in entity_map.items()}
    out: list[dict] = []

    def add(qid_slug: str, entity: str, question: str, gold: list[dict], answer: str, lang: str) -> None:
        if any(f["fact_id"] in overridden for f in gold):
            return  # keep multi-hop membership answers identical across both corpora
        out.append({
            "qa_id": qid_slug, "entity": entity, "question": question,
            "gold_fact_ids": [f["fact_id"] for f in gold], "gold_answer": answer,
            "tags": {"modality": "html", "lang": lang, "hop": "multi", "difficulty": "hard"},
        })

    # same brand (food)
    brands: dict[str, list[str]] = defaultdict(list)
    for e, preds in by_entity.items():
        if "brand" in preds:
            brands[str(preds["brand"]["value"])].append(e)
    for brand, ents in brands.items():
        if len(ents) < 2:
            continue
        for a in ents:
            sibs = [b for b in ents if b != a]
            gold = [by_entity[a]["brand"]] + [by_entity[b]["brand"] for b in sibs] \
                 + [by_entity[b]["product_name"] for b in sibs]
            add(f"food-multihop-brand_{a.split('/')[1][-6:]}-01", a,
                f"Which other product in this catalog comes from the same brand as {name[a]}?",
                gold, ", ".join(name[b] for b in sibs), "en")
        a, b = ents[0], ents[1]
        if "allergens" in by_entity[a] and "allergens" in by_entity[b]:
            milk = [e for e in (a, b) if "milk" in str(by_entity[e]["allergens"]["value"])]
            if len(milk) == 1:
                gold = [by_entity[e]["brand"] for e in (a, b)] + [by_entity[e]["allergens"] for e in (a, b)]
                add("food-multihop-brand_allergen-01", milk[0],
                    f"Among the {brand} products in this catalog, which one contains milk?",
                    gold, name[milk[0]], "en")

    # same region (places, province level)
    regions: dict[str, list[str]] = defaultdict(list)
    for e, preds in by_entity.items():
        if "located_in_region" in preds:
            regions[str(preds["located_in_region"]["value"]).split()[0]].append(e)
    for ridx, (region, ents) in enumerate(sorted(regions.items()), start=1):
        if len(ents) < 3:
            continue
        rslug = f"region{ridx}"  # qa_id must stay ASCII (schema pattern)
        region_gold = [by_entity[e]["located_in_region"] for e in ents]

        def members(pred: str, test) -> tuple[list[str], list[dict]]:
            hit = [e for e in ents if pred in by_entity[e] and test(str(by_entity[e][pred]["value"]))]
            return hit, [by_entity[e][pred] for e in ents if pred in by_entity[e]]

        hit, gold_p = members("closed_days", lambda v: "월요일" in v)
        if hit:
            add(f"place-multihop-{rslug}_monday-01", hit[0],
                f"{region}에 있는 장소들 중 매주 월요일에 쉬는 곳은 어디인가?",
                region_gold + gold_p, ", ".join(name[e] for e in hit), "ko")
        hit, gold_p = members("opening_hours", lambda v: "상시" not in v)
        if hit:
            add(f"place-multihop-{rslug}_hours-01", hit[0],
                f"{region}의 장소들 중 상시 개방이 아닌(운영 시간이 정해진) 곳을 모두 알려줘.",
                region_gold + gold_p, ", ".join(name[e] for e in hit), "ko")
        hit, gold_p = members("closed_days", lambda v: "연중무휴" in v)
        if hit:
            add(f"place-multihop-{rslug}_noholiday-01", hit[0],
                f"{region}에 있는 장소들 중 연중무휴로 운영되는 곳은 어디인가?",
                region_gold + gold_p, ", ".join(name[e] for e in hit), "ko")
    return out


# ---------------------------------------------------------------------------
# draft / finalize
# ---------------------------------------------------------------------------


def run_draft(batch: str | None = None, entities_file: Path | None = None) -> None:
    """batch mode (expansion, 2026-07-15): draft ONLY the entities listed in
    entities_file, with the language-flip counter and image-share target scoped to
    this batch, and skip multi-hop. Existing qa.jsonl / master draft & review files
    are untouched — global counters over a grown corpus would silently reshuffle
    the lang/modality of already-reviewed items. Outputs go to
    work/expansion/<batch>/. Multi-hop is regenerated at version milestones only."""
    only: set[str] | None = None
    if batch:
        only = {l.strip() for l in entities_file.read_text(encoding="utf-8").splitlines() if l.strip()}
    facts, entity_map, placement, ambiguous, img_excl, overridden = _load()
    by_page: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for f in facts:
        if only is not None and f["entity"] not in only:
            continue
        by_page[(f["entity"], f["linktype"])].append(f)
    media_by_source = {m["source_id"]: [x["file"] for x in v.get("media", [])]
                       for m, v in ((entity_map[e], v) for e, v in
                                    _manifest()["entities"].items())}

    qas: list[dict] = []
    page_no = 0
    for (entity, linktype), page_facts in sorted(by_page.items()):
        meta = entity_map[entity]
        cls, ename = meta["class"], meta["name"]
        gold_pool = [f for f in page_facts if f["fact_id"] not in ambiguous and f["predicate"] != "overview"]
        if not gold_pool:
            continue
        page_no += 1
        doc_lang = get_domain(cls).page_language
        flipped = page_no % 7 == 0
        q_lang = ("ko" if doc_lang == "en" else "en") if flipped else doc_lang

        for i, q in enumerate(draft_page(entity, ename, cls, linktype, gold_pool, q_lang), start=1):
            gold_ids = [g for g in q.get("gold_fact_ids", [])]
            known = {f["fact_id"] for f in gold_pool}
            if not gold_ids or not set(gold_ids) <= known:
                print(f"  drop (bad gold): {entity}/{linktype} #{i}", file=sys.stderr)
                continue
            qas.append({
                "qa_id": f"{cls}-{entity.split('/')[1]}-{linktype.lower()}-{i:02d}",
                "entity": entity, "question": str(q["question"]),
                "gold_fact_ids": gold_ids, "gold_answer": str(q["gold_answer"]),
                "tags": {"modality": "html", "lang": q_lang, "hop": "single",
                         "difficulty": q.get("difficulty", "easy"),
                         **({"cross_lingual": True} if flipped else {})},
            })

    if only is None:
        qas.extend(multihop(facts, entity_map, overridden))
    else:
        print("batch mode: multi-hop 생략 (버전 마일스톤에서 전체 재생성)")

    # modality=image re-tagging: nutrition-value QAs whose label photo agrees with the text
    facts_by_id = {f["fact_id"]: f for f in facts}
    eligible = []
    for qa in qas:
        if not qa["qa_id"].startswith("food") or "nutritionalinformation" not in qa["qa_id"]:
            continue
        src_id = entity_map[qa["entity"]]["source_id"]
        preds = {facts_by_id[g]["predicate"].rsplit("_", 1)[0] for g in qa["gold_fact_ids"]}
        if any((src_id, p) in img_excl for p in preds):
            continue
        if "nutrition-label.jpg" in media_by_source.get(src_id, []):
            eligible.append(qa)
    target = int(len(qas) * IMAGE_SHARE)
    for qa in eligible[:target]:
        qa["tags"]["modality"] = "image"
    n_img = sum(q["tags"]["modality"] == "image" for q in qas)
    if n_img < target:
        print(f"WARN modality=image {n_img}/{target} (제외 목록·적격 문항 부족)")
    print("WARN modality=video 0건 — v1 코퍼스에 영상 미디어 없음 (설계서 §S6 한계)")

    for qa in qas:
        _QA_VALIDATOR.validate(qa)
    draft_path, review_csv = DRAFT_PATH, REVIEW_CSV
    if batch:
        out_dir = WORK_DIR / "expansion" / batch
        out_dir.mkdir(parents=True, exist_ok=True)
        draft_path, review_csv = out_dir / "qa-draft.jsonl", out_dir / "qa-review.csv"
    draft_path.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in qas), encoding="utf-8")

    with review_csv.open("w", newline="", encoding="utf-8-sig") as fh:  # BOM so Excel reads the Korean columns
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for qa in qas:
            w.writerow({
                "qa_id": qa["qa_id"], "entity_name": entity_map[qa["entity"]]["name"],
                "linktype": qa["qa_id"].split("-")[-2], "modality": qa["tags"]["modality"],
                "lang": qa["tags"]["lang"], "hop": qa["tags"]["hop"], "difficulty": qa["tags"]["difficulty"],
                "question": qa["question"], "gold_answer": qa["gold_answer"],
                "gold_fact_ids": ";".join(qa["gold_fact_ids"]),
                "evidence_page": ";".join(sorted({placement.get(g, "?") for g in qa["gold_fact_ids"]})),
                "verdict": "", "edited_question": "", "edited_answer": "",
            })

    stats = defaultdict(int)
    for qa in qas:
        t = qa["tags"]
        stats[f"modality:{t['modality']}"] += 1
        stats[f"lang:{t['lang']}"] += 1
        stats[f"hop:{t['hop']}"] += 1
        stats[f"difficulty:{t['difficulty']}"] += 1
    print(f"{len(qas)} drafts -> {draft_path}\n검수 시트 -> {review_csv}")
    print("  " + " | ".join(f"{k}={v}" for k, v in sorted(stats.items())))


def run_finalize() -> None:
    drafts = {json.loads(l)["qa_id"]: json.loads(l)
              for l in DRAFT_PATH.read_text(encoding="utf-8").splitlines() if l.strip()}
    final, pending = [], []
    with REVIEW_CSV.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            verdict = (row["verdict"] or "").strip().lower()
            if verdict not in ("approved", "edited", "rejected"):
                pending.append(row["qa_id"])
                continue
            if verdict == "rejected":
                continue
            qa = drafts[row["qa_id"]]
            if verdict == "edited":
                if row["edited_question"].strip():
                    qa["question"] = row["edited_question"].strip()
                if row["edited_answer"].strip():
                    qa["gold_answer"] = row["edited_answer"].strip()
            qa["review"] = verdict
            _QA_VALIDATOR.validate(qa)
            final.append(qa)
    if pending:
        sys.exit(f"검수 미완료 {len(pending)}건 (verdict 비어 있음): {pending[:5]} …")
    FINAL_PATH.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in final), encoding="utf-8")
    print(f"{len(final)} QA -> {FINAL_PATH} (rejected {len(drafts) - len(final)}건 제외)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["draft", "finalize"])
    parser.add_argument("--batch", help="expansion batch name, e.g. day-01 (draft only)")
    parser.add_argument("--entities-file", type=Path,
                        help="file with one entity id (e.g. 01/00000012345678) per line")
    args = parser.parse_args()
    if args.command == "draft":
        if bool(args.batch) != bool(args.entities_file):
            sys.exit("--batch and --entities-file must be used together")
        run_draft(args.batch, args.entities_file)
    else:
        run_finalize()


if __name__ == "__main__":
    main()
