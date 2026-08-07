"""대량 확장 — 의약품(openFDA 라벨) 수확기 (docs/11).

    python -m scripts.bulk.pharma_harvest scan                        # 덤프 14파트 -> UPC 보유 창고 + 통계
    python -m scripts.bulk.pharma_harvest select --batch pharma-pilot # 파일럿 목록 -> 스냅샷·selection 등록

OFF 수확기(off_harvest)와 같은 패턴: scan은 덤프 전용(API 0회), 원본 줄 전체를 창고에 보존.
select는 확정 후보 파일(work/bulk/pharma-pilot-20.jsonl — 2026-07-31 사용자 승인 20개)을
읽어 ① data/raw/openfda/label-{upc}.json 스냅샷(사용 필드+DailyMed 이미지 URL 동봉)
② selection.yaml `pharma:` 절(파일 끝 append — food/place 삽입 지점과 무충돌)
③ candidates-pharma.jsonl ④ expansion/{batch}-entities.txt 를 쓴다.
대량 select(게이트→선별)는 docs/17 확정 후 확장 예정.

덤프: benchmark/data/dump/openfda-label/drug-label-00NN-of-0014.json.zip
     (https://open.fda.gov/apis/downloads/ 벌크, export 2026-07-29, 261,077 라벨)
창고 게이트: openfda.upc 존재 — GTIN이 있어야 DL URI(01/{gtin})에 태울 수 있다.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import zipfile
from collections import Counter

from scripts.common.config import DATA_DIR, RAW_PHARMA_DIR, WORK_DIR, ensure_dirs
from scripts.common.fetch import cached_json
from scripts.common.identifiers import gtin_to_14, is_valid

BULK_DIR = WORK_DIR / "bulk"
DUMP_DIR = DATA_DIR / "dump" / "openfda-label"
#: UPC 보유 라벨의 덤프 원본 전체 보존 창고 (off-raw-matched와 같은 역할)
RAW_MATCHED = BULK_DIR / "pharma-raw-matched.jsonl.gz"
PILOT = BULK_DIR / "pharma-pilot-20.jsonl"
SELECTION = WORK_DIR / "selection.yaml"
CAND_PHARMA = WORK_DIR / "candidates-pharma.jsonl"

#: 충실도 리포트 대상 핵심 텍스트 필드 (docs/11 §3)
CORE_FIELDS = ["indications_and_usage", "dosage_and_administration", "warnings",
               "active_ingredient", "inactive_ingredient", "purpose",
               "storage_and_handling", "questions"]

#: 스냅샷에 담는 라벨 본문 필드 (docs/11 부록 A의 ✓·△ 소비자 라벨 섹션)
SNAPSHOT_FIELDS = [
    "purpose", "indications_and_usage", "dosage_and_administration", "warnings",
    "active_ingredient", "inactive_ingredient", "storage_and_handling", "questions",
    "do_not_use", "stop_use", "when_using", "ask_doctor", "ask_doctor_or_pharmacist",
    "keep_out_of_reach_of_children", "pregnancy_or_breast_feeding",
    "package_label_principal_display_panel",
]
OPENFDA_KEEP = ["brand_name", "generic_name", "manufacturer_name", "product_type",
                "route", "substance_name", "upc", "is_original_packager",
                "spl_set_id", "product_ndc"]
DAILYMED_MEDIA = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{sid}/media.json"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _first(rec: dict, key: str) -> str:
    v = (rec.get("openfda") or {}).get(key) or []
    return str(v[0]) if v else ""


def run_scan() -> None:
    ensure_dirs()
    BULK_DIR.mkdir(parents=True, exist_ok=True)
    parts = sorted(DUMP_DIR.glob("drug-label-*.json.zip"))
    if len(parts) != 14:
        raise SystemExit(f"덤프 파트 {len(parts)}/14개 — 다운로드 확인: {DUMP_DIR}")

    total = kept = upc_valid = 0
    fill: Counter[str] = Counter()
    dedup_keys: set[str] = set()
    brands: Counter[str] = Counter()
    makers: Counter[str] = Counter()
    ptypes: Counter[str] = Counter()
    with gzip.open(RAW_MATCHED, "wt", encoding="utf-8") as out:
        for zp in parts:
            with zipfile.ZipFile(zp) as z:
                name = z.namelist()[0]
                with z.open(name) as f:
                    rows = json.load(io.TextIOWrapper(f, encoding="utf-8"))["results"]
            n_part = 0
            for rec in rows:
                total += 1
                upcs = (rec.get("openfda") or {}).get("upc") or []
                if not upcs:
                    continue
                kept += 1
                n_part += 1
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if any(is_valid(str(u)) for u in upcs):
                    upc_valid += 1
                for k in CORE_FIELDS:
                    if rec.get(k):
                        fill[k] += 1
                brand, generic = _first(rec, "brand_name"), _first(rec, "generic_name")
                dedup_keys.add(f"{_norm(brand)}|{_norm(generic)}")
                brands[brand] += 1
                makers[_norm(_first(rec, "manufacturer_name"))] += 1
                ptypes[_first(rec, "product_type") or "?"] += 1
            print(f"{zp.name}: {len(rows):,}행 중 UPC 보유 {n_part:,}", flush=True)

    print(f"\nscan: {total:,}라벨 → UPC 보유 {kept:,} -> {RAW_MATCHED}")
    print(f"  GTIN 체크섬 유효: {upc_valid:,}/{kept:,} ({100 * upc_valid / kept:.0f}%)")
    print("  핵심 필드 채움(창고 기준):")
    for k in CORE_FIELDS:
        print(f"    {k:28s} {fill[k]:6,} ({100 * fill[k] / kept:.0f}%)")
    print(f"  중복 제거 미리보기: 브랜드+성분 키 {len(dedup_keys):,}종 (라벨 {kept:,}개)")
    print("  브랜드 상위 10:", ", ".join(f"{b or '?'}×{c}" for b, c in brands.most_common(10)))
    print("  제조사(정규화) 상위 10:", ", ".join(f"{m or '?'}×{c}" for m, c in makers.most_common(10)))
    print("  제품 유형:", dict(ptypes))


def run_select(batch: str) -> None:
    """파일럿 확정본 20개를 파이프라인에 등록한다 (멱등 — 이미 등록된 UPC는 건너뜀)."""
    ensure_dirs()
    rows = [json.loads(l) for l in PILOT.read_text(encoding="utf-8").splitlines() if l.strip()]
    existing: set[str] = set()
    if SELECTION.exists():
        import yaml
        existing = {str(x) for x in (yaml.safe_load(SELECTION.read_text(encoding="utf-8")) or {}).get("pharma") or []}

    sel_lines: list[str] = []
    cand_lines: list[str] = []
    ent_lines: list[str] = []
    for row in rows:
        upc, sid = row["upc"], row["set_id"]
        rec = row["record"]
        of = rec.get("openfda") or {}
        # 스냅샷: 사용 필드 + DailyMed 이미지 URL 동봉 (build_fixtures가 읽음)
        media_raw = cached_json(RAW_PHARMA_DIR / f"media-{sid}.json", DAILYMED_MEDIA.format(sid=sid))
        media = [{"name": m.get("name"), "url": m.get("url"), "mime_type": m.get("mime_type")}
                 for m in (media_raw.get("data") or {}).get("media") or [] if m.get("url")]
        snap = {
            "upc": upc, "set_id": sid,
            "effective_time": rec.get("effective_time"), "version": rec.get("version"),
            "label": {k: rec[k] for k in SNAPSHOT_FIELDS if rec.get(k)},
            "openfda": {k: of[k] for k in OPENFDA_KEEP if of.get(k) is not None},
            "media": media,
        }
        snap_path = RAW_PHARMA_DIR / f"label-{upc}.json"
        if not snap_path.exists():
            snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        if upc in existing:
            continue
        brand = row["brand"]
        sel_lines.append(f'  - "{upc}"   # {brand[:40]} — {row["manufacturer"][:30]}')
        cand_lines.append(json.dumps({
            "id": upc, "name": brand, "generic": row["generic"],
            "manufacturer": row["manufacturer"], "set_id": sid,
            "images": len(media), "family": row.get("family"),
        }, ensure_ascii=False))
        ent_lines.append(f"01/{gtin_to_14(upc)}")

    if not sel_lines:
        print("select: 신규 등록 0 (전부 기존)")
        return
    # selection.yaml — pharma: 절은 파일 끝 append. food(place: 앞 삽입)·place(place_excluded:
    # 앞 삽입)의 구조 가정과 충돌하지 않는 유일한 위치다.
    text = SELECTION.read_text(encoding="utf-8")
    if "\npharma:" not in text:
        text = text.rstrip("\n") + "\n\npharma:  # openFDA OTC 라벨 (UPC=GTIN, docs/11)\n"
    text = text.rstrip("\n") + f"\n  # batch {batch} (+{len(sel_lines)})\n" + "\n".join(sel_lines) + "\n"
    SELECTION.write_text(text, encoding="utf-8")
    with CAND_PHARMA.open("a", encoding="utf-8") as f:
        f.write("\n".join(cand_lines) + "\n")
    ent_file = WORK_DIR / "expansion" / f"{batch}-entities.txt"
    ent_file.parent.mkdir(parents=True, exist_ok=True)
    prev = ent_file.read_text(encoding="utf-8") if ent_file.exists() else ""
    ent_file.write_text(prev + "\n".join(ent_lines) + "\n", encoding="utf-8")
    print(f"select: {len(sel_lines)}개 등록 -> selection.yaml pharma: / {CAND_PHARMA.name} / {ent_file.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["scan", "select"])
    ap.add_argument("--batch", default="pharma-pilot")
    a = ap.parse_args()
    if a.command == "scan":
        run_scan()
    else:
        run_select(a.batch)


if __name__ == "__main__":
    main()
