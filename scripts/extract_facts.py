"""S3 — extract atomic facts from cached sources into facts.jsonl.

    python -m scripts.extract_facts                # rule-based + overview span mining
    python -m scripts.extract_facts --verify-images  # + VLM cross-check of nutrition labels

Rule-based mapping is the backbone. Two LLM assists:
  * overview span mining (places): pull verifiable facts out of the prose so QA
    can target values embedded in a passage (fact carries passage + value_span).
  * --verify-images (food): read the nutrition-label photo with a VLM and compare
    against OFF's text fields — crowdsourced typos surface in work/fact-conflicts.md.
LLM outputs are disk-cached (data/raw/llm/) so re-runs are deterministic and free.

Also writes work/entity-map.json: aiPath -> {class, source_id, name} (S2 identifier
assignment lives here: food keeps its real GTIN, places get a 952-prefix demo GLN
numbered by selection order).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema.validators import validator_for

from scripts.common.config import (
    BENCH_ROOT, DATA_DIR, OPENAI_API_KEY, SCHEMA_DIR, WORK_DIR, ensure_dirs,
)
from scripts.common.fetch import get_bytes
from scripts.common.llm import llm_json as _llm_json
from scripts.common.identifiers import gtin_to_14, make_demo_gln
from scripts.clients import off_client, tour_client

FACTS_PATH = BENCH_ROOT / "facts.jsonl"
FACTS_PRETTY = BENCH_ROOT / "facts.pretty.json"
ENTITY_MAP = WORK_DIR / "entity-map.json"
CONFLICTS = WORK_DIR / "fact-conflicts.md"
SELECTION = WORK_DIR / "selection.yaml"

FACT_SCHEMA = json.loads((SCHEMA_DIR / "fact.schema.json").read_text(encoding="utf-8"))
#: 검증기 1회 컴파일 (2026-08-05 감사 F1) — jsonschema.validate()는 호출마다 스키마를
#: 재컴파일해서 35만 팩트에 16.8분을 태우고 있었다. 사전 컴파일이면 12초.
_FACT_VALIDATOR = validator_for(FACT_SCHEMA)(FACT_SCHEMA)

MINE_MODEL = "gpt-4o-mini"
VLM_MODEL = "gpt-4o-mini"

#: nutriments we carry (per 100g and, when present, per serving).
NUTRIMENT_KEYS = [
    "energy-kcal", "fat", "saturated-fat", "carbohydrates", "sugars",
    "fiber", "proteins", "salt", "sodium",
]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _clean_tags(tags: list[str] | None) -> list[str]:
    return [re.sub(r"^[a-z]{2}:", "", t).replace("-", " ") for t in (tags or [])]


#: Literal "no data" placeholders typed into OFF by contributors (2026-07-20).
#: "none" is NOT junk — for allergens/traces it is a meaningful negative claim.
_JUNK_VALUE = re.compile(r"^\s*(not indicated\.?|n/?a|unknown|unspecified|-)\s*$", re.IGNORECASE)

#: TourAPI intro fields (usetime/restdate/...) embed literal <br> tags (2026-07-21).
_BR_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _strip_br(value: str | None) -> str | None:
    if not value:
        return value
    v = _BR_TAG.sub("\n", value)
    v = re.sub(r"[ \t]*\n[ \t]*", "\n", v)
    return re.sub(r"\n{2,}", "\n", v).strip()


def _fact(entity: str, cls: str, code: str, predicate: str, value: Any, linktype: str,
          origin: str, field: str, **src_extra: Any) -> dict[str, Any]:
    return {
        "fact_id": f"{cls}-{code}-{_slug(predicate)}",
        "entity": entity,
        "predicate": predicate,
        "value": value,
        "linktype": linktype,
        "source": {"origin": origin, "field": field, **src_extra},
    }


# ---------------------------------------------------------------------------
# food (OFF)
# ---------------------------------------------------------------------------


def food_facts(gtin: str) -> tuple[str, str, list[dict[str, Any]]]:
    p = off_client.get_product(gtin)
    if not p:
        raise RuntimeError(f"OFF has no product for {gtin}")
    entity = f"01/{gtin_to_14(gtin)}"
    name = p.get("product_name") or gtin
    F: list[dict[str, Any]] = []

    def add(pred: str, value: Any, linktype: str, field: str, **extra: Any) -> None:
        # OFF crowd data sometimes carries literal placeholders ("Not indicated.",
        # "Unspecified") — absence typed as text. Not real facts: drop them the same
        # as empty fields (user decision 2026-07-20; Belbake/Rivercote/Kinder cases).
        if isinstance(value, str) and _JUNK_VALUE.match(value):
            return
        if isinstance(value, list):
            value = [v for v in value if not (isinstance(v, str) and _JUNK_VALUE.match(v))]
        if value not in (None, "", [], {}):
            F.append(_fact(entity, "food", gtin, pred, value, linktype, "off", field, **extra))

    # pip — identity + summary only (unique-placement: numbers live on their own pages)
    add("product_name", name, "pip", "product_name")
    brand = (p.get("brands") or "").split(",")[0].strip()
    if brand:
        f = _fact(entity, "food", gtin, "brand", brand, "pip", "off", "brands")
        f["relation"] = True  # same-brand multi-hop material
        F.append(f)
    add("categories", _clean_tags(p.get("categories_tags"))[:5], "pip", "categories_tags")
    # product specs/classification → pip per gs1:pip ("product description, specifications")
    add("quantity", p.get("quantity"), "pip", "quantity")
    if p.get("food_groups"):
        add("food_group", _clean_tags([p["food_groups"]])[0], "pip", "food_groups")
    add("nova_group", p.get("nova_group"), "pip", "nova_group")  # processing class 1-4, not a nutrient
    # packaging-facility codes have no clean linktype (traceability needs richer
    # provenance) — default to pip per "don't lose information" rule, remappable later
    emb = [x.strip() for x in (p.get("emb_codes") or "").split(",") if x.strip()] \
        or _clean_tags(p.get("emb_codes_tags"))
    add("packaging_facility_codes", emb, "pip",
        "emb_codes" if p.get("emb_codes") else "emb_codes_tags")

    # nutritionalInfo — 100g and per-serving side by side (difficulty knob 3)
    nutr = p.get("nutriments") or {}
    for key in NUTRIMENT_KEYS:
        for basis in ("100g", "serving"):
            amount = nutr.get(f"{key}_{basis}")
            if amount is None:
                continue
            unit = nutr.get(f"{key}_unit") or ("kcal" if key == "energy-kcal" else "g")
            add(f"{key.replace('-', '_')}_{basis}", {"amount": amount, "unit": unit},
                "nutritionalInfo", f"nutriments.{key}_{basis}")
    add("serving_size", p.get("serving_size"), "nutritionalInfo", "serving_size")
    # front-of-pack nutrition grade; "unknown"/"not-applicable" are absence typed as text
    if (p.get("nutriscore_grade") or "").lower() in ("a", "b", "c", "d", "e"):
        add("nutriscore_grade", p["nutriscore_grade"].upper(), "nutritionalInfo", "nutriscore_grade")

    # allergenInfo — allergens + may-contain traces only (GS1 official name; the
    # ingredient list moved to its own gs1:ingredientsInfo page, 2026-07-27)
    add("allergens", _clean_tags(p.get("allergens_tags")), "allergenInfo", "allergens_tags")
    add("traces", _clean_tags(p.get("traces_tags")), "allergenInfo", "traces_tags")

    # ingredientsInfo — full ingredient list + additives + dietary analysis
    add("ingredients_text", p.get("ingredients_text_en") or p.get("ingredients_text"),
        "ingredientsInfo", "ingredients_text")
    add("additives", _clean_tags(p.get("additives_tags")), "ingredientsInfo", "additives_tags")
    # analysis tags carry "-unknown"/"maybe-" verdicts = absence typed as text; keep definite only
    analysis = [t for t in _clean_tags(p.get("ingredients_analysis_tags"))
                if "unknown" not in t and "maybe" not in t]
    add("ingredients_analysis", analysis, "ingredientsInfo", "ingredients_analysis_tags")

    # optional linktypes (gate counts these)
    # hasRetailers — free-text `stores` has nicer casing ("Sainsbury's"); fall back
    # to the normalized tags where only those exist (tags cover more products)
    retailers = [x.strip() for x in (p.get("stores") or "").split(",") if x.strip()] \
        or _clean_tags(p.get("stores_tags"))
    add("retailers", retailers, "hasRetailers", "stores" if p.get("stores") else "stores_tags")
    add("labels", _clean_tags(p.get("labels_tags")), "certificationInfo", "labels_tags")
    add("origins", p.get("origins") or _clean_tags(p.get("origins_tags")), "locationInfo", "origins")
    add("manufacturing_places", p.get("manufacturing_places"), "locationInfo", "manufacturing_places")
    add("purchase_places", [x.strip() for x in (p.get("purchase_places") or "").split(",") if x.strip()],
        "locationInfo", "purchase_places")
    add("storage_conditions", p.get("conservation_conditions"),
        "consumerHandlingStorageInfo", "conservation_conditions")
    # packaging material: storage page is for handling/storage instructions, so it
    # lives on the sustainability page (official gs1:sustainabilityInfo covers
    # "sustainability and recycling"; ex-recyclingInfo which is not in the GS1 voc)
    add("packaging", _clean_tags(p.get("packaging_tags")), "sustainabilityInfo", "packaging_tags")
    # Eco-Score a-plus..f; "unknown"/"not-applicable" are absence typed as text
    env = (p.get("environmental_score_grade") or "").lower()
    if env in ("a-plus", "a", "b", "c", "d", "e", "f"):
        add("environmental_score_grade", "A+" if env == "a-plus" else env.upper(),
            "sustainabilityInfo", "environmental_score_grade")
    return entity, name, F


# ---------------------------------------------------------------------------
# pharma (openFDA drug/label + DailyMed) — docs/11 부록 A의 ✓ 필드만, OTC 전용
# ---------------------------------------------------------------------------

#: SPL 원문은 필드값 앞에 섹션 제목이 1~2회 중복된다 ("Purpose Purpose Pain reliever…").
#: 필드별 알려진 제목을 앞에서만 벗겨낸다 — 본문 중간은 건드리지 않는다.
_SPL_HEADERS = {
    "purpose": ["Purpose"],
    "indications_and_usage": ["Uses", "Use"],
    "dosage_and_administration": ["Directions"],
    "warnings": ["Warnings", "Warning"],
    "active_ingredient": ["Active ingredients", "Active ingredient"],
    "inactive_ingredient": ["Inactive ingredients", "Inactive ingredient"],
    "storage_and_handling": ["Other information", "Storage and handling"],
    "questions": ["Questions or comments?", "Questions?", "Questions"],
    "do_not_use": ["Do not use"],
    "stop_use": ["Stop use and ask a doctor if", "Stop use"],
    "when_using": ["When using this product"],
    "ask_doctor": ["Ask a doctor before use if you have", "Ask a doctor before use if",
                   "Ask a doctor"],
    "ask_doctor_or_pharmacist": ["Ask a doctor or pharmacist before use if you are",
                                 "Ask a doctor or pharmacist"],
    "keep_out_of_reach_of_children": ["Keep out of reach of children"],
    "pregnancy_or_breast_feeding": ["If pregnant or breast-feeding", "Pregnancy or breast feeding"],
}


def _strip_spl_header(field: str, text: str) -> str:
    v = text.strip()
    for header in _SPL_HEADERS.get(field, []):
        for _ in range(2):  # 제목이 최대 2회 연속 중복됨 (실측: "Purpose Purpose")
            if v.lower().startswith(header.lower()):
                v = v[len(header):].lstrip(" :.-—\n")
    return v


#: (openFDA 필드, predicate, linktype) — docs/11 부록 A. safetyInfo의 OTC 하위 항목은
#: 별도 팩트로 실어 페이지에서 소제목 구획이 된다.
_PHARMA_FIELDS = [
    ("purpose", "purpose", "pip"),
    ("indications_and_usage", "indications_and_usage", "pip"),
    ("dosage_and_administration", "directions", "instructions"),
    ("warnings", "warnings", "safetyInfo"),
    ("do_not_use", "do_not_use", "safetyInfo"),
    ("stop_use", "stop_use", "safetyInfo"),
    ("when_using", "when_using", "safetyInfo"),
    ("ask_doctor", "ask_doctor", "safetyInfo"),
    ("ask_doctor_or_pharmacist", "ask_doctor_or_pharmacist", "safetyInfo"),
    ("keep_out_of_reach_of_children", "keep_out_of_reach_of_children", "safetyInfo"),
    ("pregnancy_or_breast_feeding", "pregnancy_or_breast_feeding", "safetyInfo"),
    ("active_ingredient", "active_ingredients", "ingredientsInfo"),
    ("inactive_ingredient", "inactive_ingredients", "ingredientsInfo"),
    ("storage_and_handling", "storage", "consumerHandlingStorageInfo"),
    ("questions", "contact", "support"),
]


def pharma_facts(upc: str) -> tuple[str, str, list[dict[str, Any]]]:
    snap_path = DATA_DIR / "raw" / "openfda" / f"label-{upc}.json"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    label, of = snap.get("label") or {}, snap.get("openfda") or {}
    entity = f"01/{gtin_to_14(upc)}"

    def first(k: str) -> str:
        v = of.get(k) or []
        return str(v[0]).strip() if v else ""

    name = first("brand_name") or upc
    F: list[dict[str, Any]] = []

    def add(pred: str, value: Any, linktype: str, field: str, **extra: Any) -> None:
        if value not in (None, "", [], {}):
            F.append(_fact(entity, "pharma", upc, pred, value, linktype, "openfda", field, **extra))

    # pip — 정체성. 제조사·성분명은 관계 팩트 (멀티홉: 같은 제조사 / 같은 성분·제네릭)
    add("product_name", name, "pip", "openfda.brand_name")
    manufacturer = first("manufacturer_name")
    if manufacturer:
        f = _fact(entity, "pharma", upc, "manufacturer", manufacturer, "pip",
                  "openfda", "openfda.manufacturer_name")
        f["relation"] = True
        F.append(f)
    generic = first("generic_name")
    if generic:
        f = _fact(entity, "pharma", upc, "generic_name", generic, "pip",
                  "openfda", "openfda.generic_name")
        f["relation"] = True
        F.append(f)
    add("route", first("route").capitalize(), "pip", "openfda.route")
    add("product_type", "OTC drug" if "OTC" in first("product_type") else first("product_type"),
        "pip", "openfda.product_type")

    # 라벨 본문 — 원문 유지(함량 별도 추출 안 함, 2026-07-31 사용자 결정), 섹션 제목만 정리
    for field, pred, linktype in _PHARMA_FIELDS:
        raw = label.get(field)
        if not raw:
            continue
        text = _strip_spl_header(field, str(raw[0] if isinstance(raw, list) else raw))
        add(pred, text, linktype, field)

    # 라벨 개정일(effective_time)은 팩트로 안 실음 — masterData는 빌드 산물이고,
    # 개정일 QA는 T1(낡음 실험) 설계와 함께 결정할 사안 (docs/16).
    return entity, name, F


# ---------------------------------------------------------------------------
# place (TourAPI)
# ---------------------------------------------------------------------------


def place_facts(content_id: str, gln: str) -> tuple[str, str, list[dict[str, Any]]]:
    hits = [h for h in _cached_place_base(content_id)]
    base = hits[0] if hits else {}
    common = tour_client.detail_common(content_id)
    intro = tour_client.detail_intro(content_id, base.get("contenttypeid", "12"))
    entity = f"414/{gln}"
    name = base.get("title") or common.get("title") or content_id
    F: list[dict[str, Any]] = []

    def add(pred: str, value: Any, linktype: str, field: str, **extra: Any) -> None:
        if value not in (None, "", [], {}):
            F.append(_fact(entity, "place", gln, pred, value, linktype, "tour", field, **extra))

    add("place_name", name, "pip", "title")
    overview = (common.get("overview") or "").strip()
    add("overview", overview, "pip", "overview")

    addr = " ".join(x for x in (base.get("addr1"), base.get("addr2")) if x)
    add("address", addr, "locationInfo", "addr1")
    if base.get("mapx") and base.get("mapy"):
        add("coordinates", {"lon": float(base["mapx"]), "lat": float(base["mapy"])},
            "locationInfo", "mapx/mapy")
    region = (base.get("addr1") or "").split()[:2]
    if region:
        f = _fact(entity, "place", gln, "located_in_region", " ".join(region), "locationInfo", "tour", "addr1")
        f["relation"] = True  # same-region multi-hop material
        F.append(f)

    add("opening_hours", _strip_br(intro.get("usetime")), "openingHoursInfo", "usetime")
    add("closed_days", _strip_br(intro.get("restdate")), "openingHoursInfo", "restdate")
    add("info_center", _strip_br(intro.get("infocenter")), "support", "infocenter")
    add("parking", _strip_br(intro.get("parking")), "support", "parking")

    if overview:
        F.extend(mine_overview(entity, gln, name, overview))
    return entity, name, F


_PLACE_ROWS: list[dict[str, Any]] | None = None


def _place_rows() -> list[dict[str, Any]]:
    """candidates-place.jsonl 1회 로드 캐시 — 종전엔 place 엔티티마다 전체 재파싱(O(n²) 뇌관)."""
    global _PLACE_ROWS
    if _PLACE_ROWS is None:
        cand = WORK_DIR / "candidates-place.jsonl"
        _PLACE_ROWS = [json.loads(l) for l in cand.read_text(encoding="utf-8").splitlines() if l]
    return _PLACE_ROWS


def _cached_place_base(content_id: str) -> list[dict[str, Any]]:
    """The searchKeyword/areaBasedList row for this contentid, from candidate output.

    Bulk-harvested candidates (2026-07-21+) carry the full list row under "base" —
    use it directly, zero API calls. Legacy candidates fall back to the API path.
    """
    rows = _place_rows()                                 # (2026-08-05 감사 F3) 1회 로드 캐시
    names = {r["id"]: r["name"] for r in rows}
    for r in rows:
        if r["id"] == content_id and r.get("base"):
            return [r["base"]]
    kw = names.get(content_id)
    if kw:
        hits = tour_client.search_keyword(kw, content_type_id=12)
        exact = [h for h in hits if h.get("contentid") == content_id]
        if exact:
            return exact
    for item in tour_client.area_based_list(6):
        if item.get("contentid") == content_id:
            return [item]
    return []


# ---------------------------------------------------------------------------
# LLM assists (disk-cached -> deterministic re-runs)
# ---------------------------------------------------------------------------




def mine_overview(entity: str, gln: str, name: str, overview: str) -> list[dict[str, Any]]:
    """Pull up to 4 verifiable facts out of the overview prose, with exact value spans.

    The model must return value_literal verbatim from the text; anything we cannot
    re-locate with str.find() is dropped (no span -> no fact).
    """
    prompt = (
        f"다음은 한국 관광지 '{name}'의 공식 소개문이다.\n---\n{overview}\n---\n"
        "질의응답 벤치마크의 골드 팩트로 쓸 구체적 사실을 최대 4개 추출하라. "
        "수치·연도·길이·고유명사 등 검증 가능한 것만. 각 항목: "
        'predicate(영문 snake_case), value(정규화된 값), value_literal(소개문에 문자 그대로 존재하는 부분 문자열). '
        'JSON 형식: {"facts": [{"predicate": ..., "value": ..., "value_literal": ...}]}'
    )
    key = hashlib.sha256(f"mine|{MINE_MODEL}|{overview}".encode()).hexdigest()[:24]
    data = _llm_json(key, [{"role": "user", "content": prompt}], MINE_MODEL)

    out = []
    used: set[str] = set()
    for item in data.get("facts", []):
        literal = str(item.get("value_literal") or "")
        start = overview.find(literal) if literal else -1
        pred = str(item.get("predicate") or "")
        if start < 0 or not pred:
            continue  # not verbatim -> cannot anchor a span -> drop
        value = item.get("value")
        # the model sometimes answers "not_specified" for a slot it invented
        # (소이산 ecological_path_length, 2026-07-20) — absence is not a fact
        if isinstance(value, str) and (_JUNK_VALUE.match(value) or value.strip().lower() in ("not_specified", "not specified")):
            continue
        if pred in used:
            pred = f"{pred}_{sum(p.startswith(pred) for p in used) + 1}"  # model may repeat a predicate
        used.add(pred)
        out.append(_fact(
            entity, "place", gln, pred, value, "pip",
            "tour", "overview",
            passage=overview, value_span=[start, start + len(literal)],
        ))
    return out


def _conflict_hint(off_val: float, seen: float, serving_size: str, pred: str) -> str:
    """Heuristic cause tags so human adjudication starts from a hypothesis."""
    hints = []
    if "energy" in pred:  # kJ/kcal confusion only makes sense for energy
        for a, b, who in ((off_val, seen, "OFF가 kJ를 kcal로 저장한 듯"), (seen, off_val, "라벨 판독이 kJ 열을 읽은 듯")):
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


def verify_nutrition_images(selection_food: list[str]) -> None:
    """VLM reads each nutrition-label photo; >20% disagreement with OFF text -> listed.

    Policy (user decision 2026-07-08): the TEXT fields are canonical — benchmark
    values only need internal consistency, not real-world accuracy. Disagreeing
    (gtin, predicate) pairs are NOT corrected; they are written to
    work/image-qa-exclusions.json so gen_qa never builds an image-modality QA
    whose gold contradicts what the photo actually shows.
    """
    import base64

    lines = ["# 팩트 교차확인 리포트 (영양라벨 사진 vs OFF 텍스트 필드)\n",
             "정책: 텍스트 필드가 정본 (값 교정 안 함). 아래 불일치 쌍은 이미지-modality QA에서 제외.\n"]
    exclusions: list[dict[str, str]] = []
    conflicts = 0
    for gtin in selection_food:
        p = off_client.get_product(gtin)
        url = p.get("image_nutrition_url")
        if not url:
            lines.append(f"- {gtin}: 영양라벨 사진 없음 — 확인 불가")
            continue
        b64 = base64.b64encode(get_bytes(url, image=True)).decode()
        key = hashlib.sha256(f"vlm|{VLM_MODEL}|{url}".encode()).hexdigest()[:24]
        data = _llm_json(key, [{
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


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-images", action="store_true")
    parser.add_argument("--pretty", action="store_true",
                        help="facts.pretty.json(105MB 열람용 사본)도 생성 — 기본 생략 (2026-08-05 감사 F4)")
    args = parser.parse_args()
    ensure_dirs()

    sel = yaml.safe_load(SELECTION.read_text(encoding="utf-8"))
    facts: list[dict[str, Any]] = []
    entity_map: dict[str, Any] = {}

    for gtin in sel["food"]:
        entity, name, F = food_facts(str(gtin))
        entity_map[entity] = {"class": "food", "source_id": str(gtin), "name": name}
        facts.extend(F)
        print(f"  {entity}  {name}: {len(F)} facts")

    for upc in sel.get("pharma") or []:
        entity, name, F = pharma_facts(str(upc))
        entity_map[entity] = {"class": "pharma", "source_id": str(upc), "name": name}
        facts.extend(F)
        print(f"  {entity}  {name}: {len(F)} facts")

    # place_excluded: 빌드 게이트 탈락 등으로 회수된 장소 — 줄은 그대로 두고(GLN 순서 불변)
    # 추출만 건너뛴다. 해당 GLN은 영구 결번 (2026-07-21, docs/08 교훈 #14)
    excluded = {str(x) for x in sel.get("place_excluded") or []}
    for i, cid in enumerate(sel["place"], start=1):
        if str(cid) in excluded:
            continue
        gln = make_demo_gln(i)  # numbered by selection order -> reproducible
        entity, name, F = place_facts(str(cid), gln)
        entity_map[entity] = {"class": "place", "source_id": str(cid), "name": name}
        facts.extend(F)
        print(f"  {entity}  {name}: {len(F)} facts")

    dupes = len(facts) - len({f["fact_id"] for f in facts})
    if dupes:
        sys.exit(f"duplicate fact_ids: {dupes}")
    for f in facts:
        _FACT_VALIDATOR.validate(f)

    FACTS_PATH.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in facts), encoding="utf-8")
    ENTITY_MAP.write_text(json.dumps(entity_map, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.pretty:
        # reading companion: same facts grouped entity -> linktype (--pretty 옵트인)
        pretty: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for f in facts:
            key = f"{f['entity']} — {entity_map[f['entity']]['name']}"
            row = {k: v for k, v in f.items() if k not in ("entity", "linktype")}
            pretty.setdefault(key, {}).setdefault(f["linktype"], []).append(row)
        FACTS_PRETTY.write_text(json.dumps(pretty, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(facts)} facts ({len(entity_map)} entities) -> {FACTS_PATH}")

    if args.verify_images:
        verify_nutrition_images([str(g) for g in sel["food"]])


if __name__ == "__main__":
    main()
