"""S3 — turn the cached sources into atomic facts (facts.jsonl).

    python -m scripts.extract_facts                   # rule-based extraction
    python -m scripts.extract_facts --verify-images   # + VLM check of nutrition labels

Every domain declares its own rules in scripts/domains/; this module only walks
selection.yaml, calls each domain, and writes the result. Adding a domain means
adding one module there and one line to the registry.

Also writes work/entity-map.json (aiPath -> class, source id, name), which is
where identifier assignment lands: food and pharma keep their real GTIN, places
get a 952-prefix demo GLN numbered by selection order.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import Any

import yaml
from jsonschema.validators import validator_for

from scripts.common.config import BENCH_ROOT, SCHEMA_DIR, WORK_DIR, ensure_dirs
from scripts.common.fetch import get_bytes
from scripts.common.llm import llm_json
from scripts.clients import off_client
from scripts.domains import REGISTRY

FACTS_PATH = BENCH_ROOT / "facts.jsonl"
FACTS_PRETTY = BENCH_ROOT / "facts.pretty.json"
ENTITY_MAP = WORK_DIR / "entity-map.json"
CONFLICTS = WORK_DIR / "fact-conflicts.md"
SELECTION = WORK_DIR / "selection.yaml"

FACT_SCHEMA = json.loads((SCHEMA_DIR / "fact.schema.json").read_text(encoding="utf-8"))
#: Compile once. jsonschema.validate() rebuilds the validator on every call, which
#: cost 17 minutes over 350k facts; pre-compiling makes it 12 seconds.
_FACT_VALIDATOR = validator_for(FACT_SCHEMA)(FACT_SCHEMA)

VLM_MODEL = "gpt-4o-mini"


def extract_all() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Walk selection.yaml domain by domain. Deterministic and idempotent."""
    sel = yaml.safe_load(SELECTION.read_text(encoding="utf-8"))
    facts: list[dict[str, Any]] = []
    entity_map: dict[str, Any] = {}

    for name, domain in REGISTRY.items():
        source_ids = sel.get(name) or []
        # Entities retired after their identifier was issued stay in the list so
        # the ordinal-based GLNs behind them do not shift. They are skipped, and
        # their number is never reused.
        excluded = {str(x) for x in sel.get(f"{name}_excluded") or []}
        for ordinal, source_id in enumerate(source_ids, start=1):
            source_id = str(source_id)
            if source_id in excluded:
                continue
            entity = domain.entity_of(source_id, ordinal)
            display_name, entity_facts = domain.extract(source_id, entity)
            entity_map[entity] = {"class": domain.name, "source_id": source_id,
                                  "name": display_name}
            facts.extend(entity_facts)
            print(f"  {entity}  {display_name}: {len(entity_facts)} facts")
    return facts, entity_map


def write_pretty(facts: list[dict[str, Any]], entity_map: dict[str, Any]) -> None:
    """A 105 MB reading companion: same facts, grouped entity -> linktype."""
    pretty: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for f in facts:
        key = f"{f['entity']} — {entity_map[f['entity']]['name']}"
        row = {k: v for k, v in f.items() if k not in ("entity", "linktype")}
        pretty.setdefault(key, {}).setdefault(f["linktype"], []).append(row)
    FACTS_PRETTY.write_text(json.dumps(pretty, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# VLM cross-check (food only) — run before a QA milestone, not every batch
# ---------------------------------------------------------------------------


def _conflict_hint(off_val: float, seen: float, serving_size: str, pred: str) -> str:
    """Guess why the photo and the text disagree, so a human starts from a hypothesis."""
    hints = []
    if "energy" in pred:                              # kJ/kcal mix-up only makes sense here
        for a, b, who in ((off_val, seen, "OFF가 kJ를 kcal로 저장한 듯"),
                          (seen, off_val, "라벨 판독이 kJ 열을 읽은 듯")):
            if b and abs(a / b - 4.184) / 4.184 < 0.12:
                hints.append(who)
    m = re.search(r"([\d.]+)\s*g", serving_size)
    if m and off_val:
        ratio = float(m.group(1)) / 100
        if ratio and abs(seen / off_val - ratio) / ratio < 0.2:
            hints.append(f"라벨 판독이 1회제공량({serving_size}) 열을 읽은 듯")
        if ratio and abs(off_val / max(seen, 1e-9) - ratio) / ratio < 0.2:
            hints.append("OFF가 1회제공량 값을 100g에 저장한 듯")
    return f" (추정: {'; '.join(hints)})" if hints else ""


def verify_nutrition_images(food_ids: list[str]) -> None:
    """Read each nutrition-label photo with a VLM and compare against the text.

    The text fields are canonical: benchmark values need internal consistency,
    not real-world accuracy. Disagreeing pairs are never corrected — they go to
    work/image-qa-exclusions.json so gen_qa cannot build an image question whose
    gold answer contradicts the photo.
    """
    import base64

    lines = ["# 팩트 교차확인 리포트 (영양라벨 사진 vs OFF 텍스트 필드)\n",
             "정책: 텍스트 필드가 정본 (값 교정 안 함). 아래 불일치 쌍은 이미지-modality QA에서 제외.\n"]
    exclusions: list[dict[str, str]] = []
    conflicts = 0
    for gtin in food_ids:
        p = off_client.get_product(gtin)
        url = p.get("image_nutrition_url")
        if not url:
            lines.append(f"- {gtin}: 영양라벨 사진 없음 — 확인 불가")
            continue
        b64 = base64.b64encode(get_bytes(url, image=True)).decode()
        key = hashlib.sha256(f"vlm|{VLM_MODEL}|{url}".encode()).hexdigest()[:24]
        data = llm_json(key, [{
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Read the nutrition facts panel in this photo. Return JSON with per-100g "
                    "values (numbers only, null if not shown): {\"energy_kcal_100g\": ..., "
                    "\"fat_100g\": ..., \"saturated_fat_100g\": ..., \"carbohydrates_100g\": ..., "
                    "\"sugars_100g\": ..., \"proteins_100g\": ..., \"salt_100g\": ...}")},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }], VLM_MODEL)
        nutr = p.get("nutriments") or {}
        for pred, seen in data.items():
            off_val = nutr.get(pred.replace("_100g", "").replace("_", "-") + "_100g")
            if seen is None or off_val in (None, 0):
                continue
            off_f, seen_f = float(off_val), float(seen)
            if abs(seen_f - off_f) / max(abs(off_f), 1e-9) > 0.2:
                hint = _conflict_hint(off_f, seen_f, p.get("serving_size") or "", pred)
                lines.append(f"- {gtin} {pred}: OFF={off_val} vs 라벨사진={seen}{hint}")
                exclusions.append({"source_id": gtin, "predicate": pred})
                conflicts += 1
    lines.append(f"\n불일치 {conflicts}건 → work/image-qa-exclusions.json (gen_qa가 이미지 QA에서 제외).")
    CONFLICTS.write_text("\n".join(lines), encoding="utf-8")
    (WORK_DIR / "image-qa-exclusions.json").write_text(
        json.dumps(exclusions, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"verify-images: {conflicts} image-QA exclusions -> {CONFLICTS}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-images", action="store_true")
    parser.add_argument("--pretty", action="store_true",
                        help="also write facts.pretty.json (105 MB reading copy)")
    args = parser.parse_args()
    ensure_dirs()

    facts, entity_map = extract_all()

    dupes = len(facts) - len({f["fact_id"] for f in facts})
    if dupes:
        sys.exit(f"duplicate fact_ids: {dupes}")
    for f in facts:
        _FACT_VALIDATOR.validate(f)

    FACTS_PATH.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in facts),
                          encoding="utf-8")
    ENTITY_MAP.write_text(json.dumps(entity_map, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.pretty:
        write_pretty(facts, entity_map)
    print(f"{len(facts)} facts ({len(entity_map)} entities) -> {FACTS_PATH}")

    if args.verify_images:
        sel = yaml.safe_load(SELECTION.read_text(encoding="utf-8"))
        verify_nutrition_images([str(g) for g in sel["food"]])


if __name__ == "__main__":
    main()
