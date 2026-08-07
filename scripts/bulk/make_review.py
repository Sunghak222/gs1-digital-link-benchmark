"""배치 검수 리포트 자동 생성 — 사람이 "가볍게 확인"할 파일 하나를 만든다 (docs/08 §2.3).

    python -m scripts.bulk.make_review --batch day-03

출력: work/expansion/<batch>/entity-review.md
자동 검출: 채굴 팩트 표기 상이(값이 원문에 문자 그대로 없음), 라이선스 UNVERIFIED 미디어,
이미지 3장 미만, 소개글 짧음, 카테고리·지역 편중, 남은 <br>/플레이스홀더 잔재.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from scripts.common.config import BENCH_ROOT, WORK_DIR


def read_jsonl(p: Path):
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", required=True)
    a = ap.parse_args()

    ents = [l.strip() for l in (WORK_DIR / "expansion" / f"{a.batch}-entities.txt")
            .read_text(encoding="utf-8").splitlines() if l.strip()]
    ent_set = set(ents)
    em = json.loads((WORK_DIR / "entity-map.json").read_text(encoding="utf-8"))
    manifest = json.loads((BENCH_ROOT / "manifest.json").read_text(encoding="utf-8"))
    facts = [f for f in read_jsonl(BENCH_ROOT / "facts.jsonl") if f["entity"] in ent_set]
    by_ent: dict[str, list[dict]] = {}
    for f in facts:
        by_ent.setdefault(f["entity"], []).append(f)

    # 자동 검출
    mismatch, junk, brs, thin_media, unverified = [], [], [], [], []
    for f in facts:
        src = f.get("source") or {}
        if src.get("value_span") and str(f["value"]) not in src.get("passage", ""):
            s, e = src["value_span"]
            mismatch.append((f, src["passage"][s:e]))
        v = str(f.get("value", ""))
        if "<br" in v:
            brs.append(f)
        if v.strip().lower() in ("not indicated.", "unspecified", "n/a", "unknown", "not_specified"):
            junk.append(f)
    for ent in ents:
        meta = manifest["entities"].get(ent, {})
        media = meta.get("media", [])
        if len(media) < 3:
            thin_media.append((ent, len(media)))
        for m in media:
            if "UNVERIFIED" in str(m.get("license", "")):
                unverified.append((ent, m["file"]))

    L = [f"# {a.batch} 엔티티 검수 리포트 (자동 생성)", "",
         f"엔티티 {len(ents)}개 / 팩트 {len(facts)}건. 기계 검증(validate 7종 + verify_batch)은 "
         "별도 실행 결과를 참조 — 이 파일은 **사람 판단이 필요할 수 있는 것**만 모음.", ""]

    def section(title: str, rows: list[str], note: str = "") -> None:
        L.append(f"## {title} ({len(rows)}건)")
        if note:
            L.append(note)
        L.append("")
        L.extend(rows or ["(없음)"])
        L.append("")

    section("채굴 팩트 표기 상이 — 채점이 의미로 흡수함(7/21 결정), 참고용",
            [f"- {em.get(f['entity'], {}).get('name', '?')} · {f['predicate']}: "
             f"`{str(f['value'])[:50]}` ← 원문 `{orig[:50]}`" for f, orig in mismatch])
    section("이미지 3장 미만 (이미지 QA 재료 부족)",
            [f"- {em.get(e, {}).get('name', '?')} ({e}): {n}장" for e, n in thin_media])
    section("라이선스 UNVERIFIED 미디어 (공개 전 재결정 대상)",
            [f"- {em.get(e, {}).get('name', '?')}: {f}" for e, f in unverified])
    section("잔재 — <br>/플레이스홀더 (0이어야 정상)",
            [f"- {f['fact_id']}: {str(f['value'])[:60]}" for f in brs + junk])

    # 분포
    food = [e for e in ents if em.get(e, {}).get("class") == "food"]
    place = [e for e in ents if em.get(e, {}).get("class") == "place"]
    pharma = [e for e in ents if em.get(e, {}).get("class") == "pharma"]
    L += [f"## 분포 (식품 {len(food)} / 장소 {len(place)} / 의약품 {len(pharma)})", ""]
    if pharma:
        cand_pharma = {r["id"]: r for r in read_jsonl(WORK_DIR / "candidates-pharma.jsonl")}
        makers = Counter(str(cand_pharma.get(em[e]["source_id"], {}).get("manufacturer", "?"))
                         for e in pharma)
        # 복합제는 "A, B AND C" 꼴 — 성분 단위로 쪼개야 클러스터가 보인다
        gens = Counter(
            g.strip() for e in pharma
            for g in str(cand_pharma.get(em[e]["source_id"], {}).get("generic", ""))
            .replace(" AND ", ", ").split(",") if g.strip())
        L.append("- 의약품 제조사: " + ", ".join(f"{k}×{v}" for k, v in makers.most_common(8)))
        L.append("- 의약품 성분(멀티홉 클러스터): "
                 + ", ".join(f"{k[:30]}×{v}" for k, v in gens.most_common() if v >= 2))
    cand_food = {r["id"]: r for r in read_jsonl(WORK_DIR / "candidates-food.jsonl")}
    cand_place = {r["id"]: r for r in read_jsonl(WORK_DIR / "candidates-place.jsonl")}
    # 첫 태그는 OFF 최상위 우산(plant-based 등)이라 착시가 큼(2026-07-29) —
    # 우산을 건너뛴 첫 세부 태그로 집계해야 실제 편중이 보인다.
    _umbrella = {"plant-based-foods-and-beverages", "plant-based-foods",
                 "beverages-and-beverages-preparations", "fermented-foods"}

    def _cat(e: str) -> str:
        tags = [str(t).removeprefix("en:")
                for t in cand_food.get(em[e]["source_id"], {}).get("categories") or []]
        return next((t for t in tags if t not in _umbrella), tags[0] if tags else "?")

    cats = Counter(_cat(e) for e in food)
    regs = Counter(str(cand_place.get(em[e]["source_id"], {}).get("region", "?")) for e in place)
    if food:
        L.append("- 식품 카테고리 상위: " + ", ".join(f"{k}×{v}" for k, v in cats.most_common(10)))
        heavy = [f"{k}({v})" for k, v in cats.items() if v > max(5, len(food) * 0.15)]
        if heavy:
            L.append(f"- ⚠ 카테고리 편중: {', '.join(heavy)}")
    if place:
        L.append("- 장소 지역: " + ", ".join(f"{k}×{v}" for k, v in regs.most_common()))
    L += ["", "## 엔티티 전체", "", "| # | 유형 | 이름 | 팩트 | 페이지 | 이미지 |", "|---|---|---|---|---|---|"]
    for i, e in enumerate(ents, 1):
        meta = manifest["entities"].get(e, {})
        L.append(f"| {i} | {em.get(e, {}).get('class', '?')} | {em.get(e, {}).get('name', '?')} ({e}) "
                 f"| {len(by_ent.get(e, []))} | {len(meta.get('pages', []))} | {len(meta.get('media', []))} |")

    out = WORK_DIR / "expansion" / a.batch / "entity-review.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"검수 리포트 -> {out}")
    print(f"  표기상이 {len(mismatch)} / 이미지부족 {len(thin_media)} / UNVERIFIED {len(unverified)} / 잔재 {len(brs) + len(junk)}")


if __name__ == "__main__":
    main()
