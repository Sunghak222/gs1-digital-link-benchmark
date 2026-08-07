"""대량 확장 — OFF 식품 수확기 (docs/08 §2.1). API 호출 0회, 덤프 전용.

    python -m scripts.bulk.off_harvest scan                  # [1] CSV 전량 스캔 -> 후보
    python -m scripts.bulk.off_harvest snapshot              # [2] JSONL 순회: lang=en + 스냅샷 + 원본 창고
    python -m scripts.bulk.off_harvest select --target 200 --batch day-03   # [3~5] 게이트->선별->append
    python -m scripts.bulk.off_harvest all --target 200 --batch day-03      # 전부 순서대로

교훈 게이트 (docs/08 §5): 영어권 6개국 프리필터 + JSONL lang=en 최종판정(#1),
prepared 제외 영양 8칸(#2), en:null 카테고리(#3), (브랜드+제품명) 중복(#4),
이미지 HEAD 생존 확인(#12). 스냅샷은 API와 같은 봉투로 저장 — 이후 단계 무수정.
2026-07-29: en:complete 상태 필터 제거 — 실사용 필드(라벨 이미지·성분·영양 8칸)를
직접 검사하므로 중복 보험이었고, 통과분의 99%를 잘라먹던 주범. 대신 물량 캡으로
스캔 수 상위 CAND_CAP개만 후보로 저장 (스냅샷 파일 수·창고 용량 통제).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import yaml

from scripts.common.config import DATA_DIR, RAW_OFF_DIR, USER_AGENT, WORK_DIR, ensure_dirs
from scripts.common.identifiers import gtin_to_14, is_valid
from scripts.select_entities import score_food

BULK_DIR = WORK_DIR / "bulk"
CSV_CAND = BULK_DIR / "off-csv-candidates.jsonl"
SHORTLIST = BULK_DIR / "off-shortlist.jsonl"
#: 매칭된 후보의 덤프 원본 줄 전체를 보존하는 창고 — 이후 필드 추가는 13GB 재스캔
#: 대신 이 파일에서 꺼낸다. 파이프라인은 읽지 않음 (data/raw 스냅샷이 계속 입력).
RAW_MATCHED = BULK_DIR / "off-raw-matched.jsonl.gz"
SELECTION = WORK_DIR / "selection.yaml"
CAND_FOOD = WORK_DIR / "candidates-food.jsonl"

CSV_DUMP = DATA_DIR / "dump" / "en.openfoodfacts.org.products-20260720.csv.gz"
JSONL_DUMP = DATA_DIR / "dump" / "openfoodfacts-products-20260720.jsonl.gz"

#: 영어권 프리필터 (countries_en 부분 문자열) — 최종 판정은 JSONL lang=en
COUNTRIES = ["United States", "United Kingdom", "Ireland", "Australia", "Canada", "New Zealand"]

#: 후보 상한 — 품질 필터가 아니라 물량 캡. 스캔 수(unique_scans_n) 상위만 남긴다.
#: 2026-07-31: 5000 풀 소진(누적 선정 4,568)으로 10000 상향 — "멈출 때까지 추출" 지시.
CAND_CAP = 80000  # 2026-08-05 저녁: 40000 풀 소진(r87에서 0개, 누적 4.9만) → 필터 통과 전체 커버(마지막 상향)

#: 스냅샷에 담을 필드 (off_client.PRODUCT_FIELDS와 동일 집합 + lang)
SNAPSHOT_FIELDS = [
    "code", "lang", "product_name", "brands", "categories_tags", "countries_tags",
    "nutriments", "serving_size", "nutrition_data_per",
    "allergens_tags", "traces_tags", "ingredients_text", "ingredients_text_en",
    "additives_tags", "ingredients_analysis_tags", "stores", "stores_tags",
    "labels_tags", "origins", "origins_tags", "manufacturing_places",
    "conservation_conditions", "packaging_tags",
    "image_front_url", "image_nutrition_url", "image_ingredients_url",
    "unique_scans_n", "completeness", "states_tags",
    "quantity", "food_groups", "nova_group", "nutriscore_grade",  # fact enrichment 2026-07-27
    "environmental_score_grade", "purchase_places", "emb_codes", "emb_codes_tags",
]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]+", "", (s or "").lower())


def read_jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def selected_food_ids() -> list[str]:
    sel = yaml.safe_load(SELECTION.read_text(encoding="utf-8"))
    return [str(x) for x in sel.get("food") or []]


# ---------------------------------------------------------------------------
# [1] CSV scan
# ---------------------------------------------------------------------------

def run_scan() -> None:
    csv.field_size_limit(10_000_000)
    # 바코드는 소스마다 0-패딩이 달라서(CSV 14자리 vs selection 8/13자리) 정규화 대조
    existing = {c.lstrip("0") for c in selected_food_ids()}
    total, out = 0, []
    with gzip.open(CSV_DUMP, "rt", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
        g100 = [c for c in reader.fieldnames if c.endswith("_100g") and "prepared" not in c]
        for row in reader:
            total += 1
            if total % 1_000_000 == 0:
                print(f"  ..{total:,} rows, candidates {len(out)}", flush=True)
            countries = row.get("countries_en") or ""
            if not any(c in countries for c in COUNTRIES):
                continue
            if not (row.get("image_nutrition_url") or "").strip():
                continue
            if not (row.get("ingredients_text") or "").strip():
                continue
            code = (row.get("code") or "").strip()
            if not is_valid(code) or code.lstrip("0") in existing:
                continue
            filled = sum(1 for c in g100 if (row.get(c) or "").strip())
            if filled < 8:
                continue
            out.append({
                "code": code,
                "name": (row.get("product_name") or "").strip(),
                "brands": (row.get("brands") or "").strip(),
                "countries": countries,
                "scans": int(row.get("unique_scans_n") or 0),
                "image_front_url": (row.get("image_url") or "").strip(),
                "image_ingredients_url": (row.get("image_ingredients_url") or "").strip(),
                "image_nutrition_url": (row.get("image_nutrition_url") or "").strip(),
            })
    BULK_DIR.mkdir(parents=True, exist_ok=True)
    n_passed = len(out)
    out.sort(key=lambda r: -r["scans"])
    out = out[:CAND_CAP]
    CSV_CAND.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
    print(f"scan: {total:,}행 → 필터 통과 {n_passed:,}개 → 스캔 수 상위 {len(out)}개 -> {CSV_CAND}")


# ---------------------------------------------------------------------------
# [2] JSONL pass: lang=en + snapshot
# ---------------------------------------------------------------------------

#: 구형 nutriments 키로 변환할 영양소 (extract_facts.NUTRIMENT_KEYS와 동일 집합)
_NUTRIENT_IDS = ["energy-kcal", "fat", "saturated-fat", "carbohydrates", "sugars",
                 "fiber", "proteins", "salt", "sodium"]


def _round(v: Any) -> Any:
    return round(v, 4) if isinstance(v, float) else v


def _legacy_nutriments(rec: dict[str, Any]) -> dict[str, Any]:
    """신형 nutrition 구조 → 구형 nutriments 키 (as-sold, 라벨 실측만)."""
    out: dict[str, Any] = {}
    nut = rec.get("nutrition") or {}
    agg = (nut.get("aggregated_set") or {}).get("nutrients") or {}
    for key in _NUTRIENT_IDS:
        e = agg.get(key)
        if e and e.get("source") == "packaging" and e.get("value") is not None:
            out[f"{key}_100g"] = _round(e["value"])
            if e.get("unit"):
                out[f"{key}_unit"] = e["unit"]
    for s in nut.get("input_sets") or []:
        if (s.get("per") == "serving" and s.get("source") == "packaging"
                and s.get("preparation", "as_sold") == "as_sold"):
            for key in _NUTRIENT_IDS:
                e = (s.get("nutrients") or {}).get(key)
                if e and e.get("value") is not None:
                    out.setdefault(f"{key}_serving", _round(e["value"]))
    return out

def run_snapshot() -> None:
    cand = {r["code"]: r for r in read_jsonl(CSV_CAND)}
    if not cand:
        sys.exit("먼저 scan을 실행하세요 (후보 없음)")
    # JSONL 각 줄은 {"_id":"<13자리 0패딩 바코드>", ...}로 시작 — 패딩 차이는 정규화로 흡수
    wanted_norm = {c.lstrip("0") or "0": c for c in cand}
    id_re = re.compile(r'^\{"_id":"(\d+)"')
    found: dict[str, str] = {}          # csv code -> lang
    n_written = 0
    with gzip.open(JSONL_DUMP, "rt", encoding="utf-8") as fh, \
         gzip.open(RAW_MATCHED, "wt", encoding="utf-8") as raw_out:
        for i, line in enumerate(fh, 1):
            if i % 500_000 == 0:
                print(f"  ..{i:,} records, matched {len(found)}/{len(wanted_norm)}", flush=True)
            if len(found) == len(wanted_norm):
                break
            m = id_re.match(line)
            code = wanted_norm.get((m.group(1).lstrip("0") or "0")) if m else None
            if code is None or code in found:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            lang = rec.get("lang") or "?"
            found[code] = lang
            # 창고에는 lang 무관하게 매칭 전원 보존 (원본 줄 그대로)
            raw_out.write(line if line.endswith("\n") else line + "\n")
            if lang != "en":
                continue
            product = {k: rec[k] for k in SNAPSHOT_FIELDS if k in rec}
            # 덤프는 구형 nutriments가 비어 있음(신형 nutrition 구조로 이전, 2026-07-21 발견)
            # — 라벨 실측(source=packaging)만 구형 키로 변환, 추정치(estimate)는 배제
            product["nutriments"] = _legacy_nutriments(rec)
            # 덤프 레코드에는 계산된 image_*_url이 없음 — CSV 값으로 보충
            for k in ("image_front_url", "image_ingredients_url", "image_nutrition_url"):
                if not product.get(k) and cand[code].get(k):
                    product[k] = cand[code][k]
            target = RAW_OFF_DIR / f"product-{code}.json"
            # (2026-08-05 감사 #7) "영양 빈 스냅샷 치유"는 일회성 보정 완료로 제거 —
            # 기존 파일(API 시절 포함)은 덮지 않는다.
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({"code": code, "product": product, "status": 1},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
                n_written += 1
    shortlist = [dict(cand[c], lang=found.get(c, "?")) for c in cand if found.get(c) == "en"]
    SHORTLIST.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in shortlist), encoding="utf-8")
    langs = Counter(found.values())
    print(f"snapshot: 대조 {len(found)}/{len(cand)}, lang 분포 {dict(langs)}")
    print(f"원본 창고: {RAW_MATCHED.name} {RAW_MATCHED.stat().st_size/1e6:.1f}MB ({len(found)}줄)")
    print(f"  영어 {len(shortlist)}개 -> {SHORTLIST} (스냅샷 신규 저장 {n_written})")


# ---------------------------------------------------------------------------
# [3~5] gates -> HEAD -> select -> append
# ---------------------------------------------------------------------------

def _head_alive(client: httpx.Client, url: str) -> bool:
    try:
        r = client.head(url, follow_redirects=True, timeout=10)
        if r.status_code == 405:            # HEAD 미지원 서버는 GET 1바이트
            r = client.get(url, headers={"Range": "bytes=0-0"}, timeout=10)
        return r.status_code < 400
    except httpx.HTTPError:
        return False


def _snapshot_product(code: str) -> dict[str, Any]:
    p = RAW_OFF_DIR / f"product-{code}.json"
    return json.loads(p.read_text(encoding="utf-8")).get("product") or {}


def run_select(target: int, batch: str) -> None:
    shortlist = read_jsonl(SHORTLIST)
    if not shortlist:
        sys.exit("먼저 snapshot을 실행하세요")
    existing_ids = selected_food_ids()
    # (브랜드+제품명) 정규화 중복 세트 — 기존 스냅샷 기준 (교훈 #4: 브랜드 다르면 동명 허용).
    # 2026-08-05 감사 #1: 매 라운드 스냅샷 1.9만 파일 재읽기 → 코드→키 캐시로 증분화.
    # 키 산출식은 종전과 동일 (candidates 파일로 대체는 이름 7% 불일치 실측으로 기각).
    cache_p = BULK_DIR / "seen-names-cache.json"
    key_cache: dict[str, list[str]] = {}
    if cache_p.exists():
        try:
            key_cache = json.loads(cache_p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 캐시 깨지면 스냅샷 전체 재읽기로 자연 강등
            key_cache = {}
    seen_names: set[tuple[str, str]] = set()
    cache_dirty = False
    for code in existing_ids:
        if code in key_cache:
            seen_names.add(tuple(key_cache[code]))
            continue
        try:
            p = _snapshot_product(code)
        except FileNotFoundError:
            continue
        k = (_norm((p.get("brands") or "").split(",")[0]), _norm(p.get("product_name") or ""))
        key_cache[code] = list(k)
        seen_names.add(k)
        cache_dirty = True
    if cache_dirty:
        cache_p.write_text(json.dumps(key_cache, ensure_ascii=False), encoding="utf-8")

    graded = []
    # 2026-08-05: 정규화 집합을 루프 밖에서 1회 구축 — 안에서 만들면 후보×기선정(16k×12k)
    # 곱이 되어 수 분을 태운다 (감사에서 발견된 O(n²))
    existing_norm = {c.lstrip("0") for c in existing_ids}
    for r in shortlist:
        code = r["code"]
        if code.lstrip("0") in existing_norm:
            continue                                     # 패딩 차이 방어 — 스냅샷 읽기 전에 먼저
        try:
            p = _snapshot_product(code)
        except FileNotFoundError:
            continue
        cats = p.get("categories_tags") or []
        if "en:null" in cats:
            continue                                     # 교훈 #3
        # 교훈 #2: as-sold 라벨 영양 7칸+ — prepared(조리 후) 키는 세지 않는다.
        # (day-01 시절 API 캐시가 남아 있는 제품은 스냅샷이 API 형식이라 prepared 키 존재 가능)
        n100 = [k for k in (p.get("nutriments") or {}) if k.endswith("_100g") and "prepared" not in k]
        if len(n100) < 7:
            continue
        key = (_norm((p.get("brands") or "").split(",")[0]), _norm(p.get("product_name") or ""))
        if key in seen_names or not key[1]:
            continue                                     # 교훈 #4
        graded.append({**r, "score": sum(score_food(p).values()), "score_parts": score_food(p),
                       "categories": (p.get("categories_tags") or [])[:4],
                       "allergens": p.get("allergens_tags") or [], "_key": key, "_p": p})
    graded.sort(key=lambda x: (-x["score"], -(x["scans"] or 0)))
    print(f"게이트 통과 {len(graded)}개 — 이미지 생존 확인하며 상위 {target}개 선별")

    picked, dead = [], 0
    picked_keys: set[tuple[str, str]] = set()
    # 2026-08-04: 생존 확인을 동시 8개로(개당 2.4초 → 라운드 20분이 병목이었음).
    # 확인(HEAD)만 병렬이고 판정·기록은 순위 순서대로 순차 소비 — 결과는 순차판과 동일.
    # 예의는 per-요청 sleep 대신 동시 8개 상한으로 유지.
    CHUNK = 32

    def _probe(r: dict) -> tuple[dict, bool, list[str]]:
        p = r["_p"]
        if not _head_alive(client, p["image_nutrition_url"]):
            return r, False, []                           # 영양라벨은 필수 (교훈 #12)
        deadf = [k for k in ("image_front_url", "image_ingredients_url")
                 if p.get(k) and not _head_alive(client, p[k])]
        return r, True, deadf

    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client, \
            ThreadPoolExecutor(max_workers=8) as ex:
        for i in range(0, len(graded), CHUNK):
            if len(picked) >= target:
                break
            for r, alive, deadf in ex.map(_probe, graded[i:i + CHUNK]):
                if len(picked) >= target:
                    break
                if r["_key"] in picked_keys:
                    continue
                if not alive:
                    dead += 1
                    continue
                p = r["_p"]
                # front/ingredients가 죽었으면 제품 탈락 대신 스냅샷에서 그 필드만 제거
                for k in deadf:
                    p.pop(k, None)
                if deadf:
                    path = RAW_OFF_DIR / f"product-{r['code']}.json"
                    path.write_text(json.dumps({"code": r["code"], "product": p, "status": 1},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
                picked.append(r)
                picked_keys.add(r["_key"])
                if len(picked) % 25 == 0:
                    print(f"  ..{len(picked)}/{target} (죽은 라벨 {dead})", flush=True)

    if len(picked) < target:
        print(f"WARN: 목표 {target} 중 {len(picked)}개만 확보 (풀 소진/이미지 사망 {dead})")

    # --- append: selection.yaml + candidates-food.jsonl + 배치 엔티티 목록
    before = SELECTION.read_text(encoding="utf-8")
    lines = [f"  # --- 대량 확장 {batch} (OFF 덤프 20260720, docs/08) ---"]
    for r in picked:
        brand = (r["_p"].get("brands") or "").split(",")[0].strip()
        lines.append(f'  - "{r["code"]}"   # {brand} — {r["name"][:40]}')
    place_idx = before.index("\nplace:")
    SELECTION.write_text(before[:place_idx] + "\n".join(lines) + "\n" + before[place_idx:], encoding="utf-8")

    with CAND_FOOD.open("a", encoding="utf-8") as fh:
        for r in picked:
            fh.write(json.dumps({
                "id": r["code"], "name": r["name"],
                "brand": (r["_p"].get("brands") or "").split(",")[0].strip(),
                "score": r["score"], "score_parts": r["score_parts"],
                "categories": r["categories"], "allergens": r["allergens"], "scans": r["scans"],
            }, ensure_ascii=False) + "\n")

    ents_path = WORK_DIR / "expansion" / f"{batch}-entities.txt"
    prev = ents_path.read_text(encoding="utf-8") if ents_path.exists() else ""
    ents_path.write_text(prev + "".join(f"01/{gtin_to_14(r['code'])}\n" for r in picked), encoding="utf-8")

    # 첫 태그는 OFF 최상위 우산(plant-based 등)이라 착시가 큼(7-29, make_review와 동일 처리) —
    # 우산을 건너뛴 첫 세부 태그로 집계해야 실제 편중이 보인다.
    _umbrella = {"plant-based-foods-and-beverages", "plant-based-foods",
                 "beverages-and-beverages-preparations", "fermented-foods"}

    def _cat(r: dict) -> str:
        tags = [str(t).removeprefix("en:") for t in r["categories"]]
        return next((t for t in tags if t not in _umbrella), tags[0] if tags else "?")

    cat_top = Counter(_cat(r) for r in picked).most_common(8)
    print(f"select: {len(picked)}개 append -> selection.yaml / {ents_path}")
    print("  카테고리 상위:", ", ".join(f"{k}×{v}" for k, v in cat_top))
    big = [f"{k}({v})" for k, v in cat_top if v > max(5, len(picked) * 0.15)]
    if big:
        print(f"  ⚠ 편중 경고: {', '.join(big)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["scan", "snapshot", "select", "all"])
    ap.add_argument("--target", type=int, default=200)
    ap.add_argument("--batch", default="day-03")
    a = ap.parse_args()
    ensure_dirs()
    if a.command in ("scan", "all") and not (a.command == "all" and CSV_CAND.exists()):
        run_scan()
    if a.command in ("snapshot", "all") and not (a.command == "all" and SHORTLIST.exists()):
        run_snapshot()
    if a.command in ("select", "all"):
        run_select(a.target, a.batch)


if __name__ == "__main__":
    main()
