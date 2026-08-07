"""Bulk expansion — TourAPI place harvester. Quota-aware and resumable.

    python -m scripts.bulk.tour_harvest sweep                   # nationwide list (~140 calls, then cached)
    python -m scripts.bulk.tour_harvest eval --budget 700       # detail evaluation, 3 calls per place
    python -m scripts.bulk.tour_harvest select --batch day-03   # append the passers
    python -m scripts.bulk.tour_harvest all --budget 700 --batch day-03

Gate: a 300+ character overview, three or more photos explicitly licensed KOGL
Type 1, and an address. Verdicts accumulate in work/bulk/tour-eval.jsonl, one line
per place, so a re-run never spends quota on a place it already judged. Only real
HTTP calls count against the budget; the run stops cleanly when it is exhausted.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

import scripts.common.fetch as fetch
from scripts.clients import tour_client
from scripts.common.config import WORK_DIR, ensure_dirs

BULK_DIR = WORK_DIR / "bulk"
POOL = BULK_DIR / "tour-pool.jsonl"
EVAL = BULK_DIR / "tour-eval.jsonl"
SELECTION = WORK_DIR / "selection.yaml"
CAND_PLACE = WORK_DIR / "candidates-place.jsonl"

AREA_NAMES = {1: "서울", 2: "인천", 3: "대전", 4: "대구", 5: "광주", 6: "부산", 7: "울산",
              8: "세종", 31: "경기", 32: "강원", 33: "충북", 34: "충남", 35: "경북",
              36: "경남", 37: "전북", 38: "전남", 39: "제주"}

BASE_KEEP = ["contentid", "contenttypeid", "title", "addr1", "addr2", "areacode",
             "mapx", "mapy", "firstimage", "cat1", "cat2", "cat3", "modifiedtime"]


class QuotaExhausted(RuntimeError):
    pass


def _install_counter(budget: int) -> dict[str, int]:
    """Count real HTTP GETs only; a cache hit costs no quota."""
    counter = {"n": 0, "budget": budget}
    orig = fetch.get_json

    def counting(url: str, **kw: Any):
        if counter["n"] >= counter["budget"]:
            raise QuotaExhausted(f"예산 {counter['budget']}회 소진")
        counter["n"] += 1
        return orig(url, **kw)

    fetch.get_json = counting
    return counter


def read_jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def selected_place_ids() -> list[str]:
    sel = yaml.safe_load(SELECTION.read_text(encoding="utf-8"))
    return [str(x) for x in sel.get("place") or []]


# ---------------------------------------------------------------------------
# [1] nationwide pool
# ---------------------------------------------------------------------------

def run_sweep() -> None:
    rows: dict[str, dict[str, Any]] = {r["contentid"]: r for r in read_jsonl(POOL)}
    for area in AREA_NAMES:
        page = 1
        while True:
            items = tour_client._call(
                "areaBasedList2", f"bulk-a{area}-p{page}",
                areaCode=area, contentTypeId=12, numOfRows=100, pageNo=page, arrange="Q")
            if not items:
                break
            for it in items:
                rows[it["contentid"]] = {k: it.get(k) for k in BASE_KEEP}
            if len(items) < 100:
                break
            page += 1
        print(f"  {AREA_NAMES[area]}: 누적 {len(rows)}", flush=True)
    BULK_DIR.mkdir(parents=True, exist_ok=True)
    POOL.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows.values()), encoding="utf-8")
    print(f"sweep: 전국 풀 {len(rows)}곳 -> {POOL}")


# ---------------------------------------------------------------------------
# [3] detail evaluation until budget
# ---------------------------------------------------------------------------

def _interleave_by_area(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin across regions for a naturally spread pool, not a forced quota."""
    by_area: dict[Any, list[dict[str, Any]]] = {}
    for r in rows:
        by_area.setdefault(r.get("areacode"), []).append(r)
    out, i = [], 0
    while any(by_area.values()):
        for a in list(by_area):
            if by_area[a]:
                out.append(by_area[a].pop(0))
        i += 1
    return out


def run_eval(budget: int) -> None:
    pool = read_jsonl(POOL)
    if not pool:
        sys.exit("먼저 sweep을 실행하세요")
    done = {r["id"] for r in read_jsonl(EVAL) if r.get("verdict") != "api_error"}  # errors are retried
    skip = done | set(selected_place_ids())
    todo = [r for r in pool if r["contentid"] not in skip]
    # Places with a representative photo first: a free signal that photos exist,
    # though not that they are Type 1.
    todo = _interleave_by_area([r for r in todo if r.get("firstimage")]) \
         + _interleave_by_area([r for r in todo if not r.get("firstimage")])
    counter = _install_counter(budget)
    print(f"eval: 미평가 {len(todo)}곳, 예산 {budget}회 (장소당 3회)")

    n_pass = 0
    try:
        with EVAL.open("a", encoding="utf-8") as fh:
            for r in todo:
                cid = r["contentid"]
                try:
                    common = tour_client.detail_common(cid)
                    intro = tour_client.detail_intro(cid, r.get("contenttypeid") or "12")
                    gallery = tour_client.detail_images(cid)
                except tour_client.TourApiError as exc:
                    fh.write(json.dumps({"id": cid, "verdict": "api_error", "err": str(exc)[:80]},
                                        ensure_ascii=False) + "\n")
                    continue
                overview = (common.get("overview") or "").strip()
                kogl1 = [g for g in gallery if g.get("cpyrhtDivCd") == "Type1"]
                # Mirrors the build gate: opening-hours fields and support fields must
                # both be present, otherwise those two optional pages never appear and
                # the entity cannot reach three optional pages.
                has_hours = bool(intro.get("usetime") or intro.get("restdate"))
                has_support = bool(intro.get("infocenter") or intro.get("parking"))
                ok = (len(overview) >= 300 and len(kogl1) >= 3 and bool(r.get("addr1"))
                      and has_hours and has_support)
                rec = {
                    "id": cid, "verdict": "pass" if ok else "fail",
                    "name": r.get("title"), "region": AREA_NAMES.get(int(r.get("areacode") or 0), "?"),
                    "overview_chars": len(overview), "kogl1": len(kogl1), "gallery": len(gallery),
                    "usetime": bool(intro.get("usetime")), "has_hours": has_hours,
                    "has_support": has_support, "cat3": r.get("cat3"),
                    "base": r,
                }
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if ok:
                    n_pass += 1
                    if n_pass % 10 == 0:
                        print(f"  ..호출 {counter['n']}, 통과 {n_pass}", flush=True)
    except QuotaExhausted:
        print(f"예산 소진 — 오늘은 여기까지 (내일 같은 명령으로 이어짐)")
    evald = read_jsonl(EVAL)
    stats = Counter(r["verdict"] for r in evald)
    print(f"eval: 호출 {counter['n']}회 사용, 누적 평가 {len(evald)}곳, 판정 {dict(stats)}")


# ---------------------------------------------------------------------------
# [5] select multiples of 10 -> append
# ---------------------------------------------------------------------------

def run_select(batch: str) -> None:
    orig_ids = selected_place_ids()                      # order before appending
    orig_set = set(orig_ids)                             # built once, not per row
    passed = [r for r in read_jsonl(EVAL) if r["verdict"] == "pass" and r["id"] not in orig_set]
    # Passes recorded before the gate changed are re-checked against cached intro
    # data, which costs no API calls.
    def _rich(r: dict[str, Any]) -> bool:
        if "has_hours" in r:
            return r["has_hours"] and r["has_support"]
        try:
            intro = tour_client.detail_intro(r["id"], (r.get("base") or {}).get("contenttypeid") or "12")
        except tour_client.TourApiError:
            return False
        return bool(intro.get("usetime") or intro.get("restdate")) and \
               bool(intro.get("infocenter") or intro.get("parking"))
    passed = [r for r in passed if _rich(r)]
    take = (len(passed) // 10) * 10
    if take == 0:
        sys.exit(f"통과 {len(passed)}곳 — 10곳 미만이라 이월 (내일 이어서)")
    picked, rest = passed[:take], passed[take:]
    print(f"select: 통과 {len(passed)} → {take}곳 선정 (+이월 {len(rest)})")

    n_before = len(orig_ids)
    before = SELECTION.read_text(encoding="utf-8")
    lines = [f"  # --- 대량 확장 {batch} (전국 수확, Type1 게이트, docs/08) ---"]
    for r in picked:
        lines.append(f'  - "{r["id"]}"   # {r["name"]} ({r["region"]}) — Type1 {r["kogl1"]}장, 소개 {r["overview_chars"]}자')
    # The place list does not always end at the file end: a place_excluded block may
    # follow. Insert above that block and its leading comment, or the list breaks.
    all_lines = before.rstrip("\n").split("\n")
    cut = next((i for i, l in enumerate(all_lines) if l.startswith("place_excluded:")), len(all_lines))
    while cut > 0 and (all_lines[cut - 1].lstrip().startswith("#") or not all_lines[cut - 1].strip()):
        cut -= 1
    tail = ([""] + all_lines[cut:]) if cut < len(all_lines) else []
    SELECTION.write_text("\n".join(all_lines[:cut] + lines + tail) + "\n", encoding="utf-8")

    # GLNs are issued by position, so the existing prefix must be untouched.
    after_ids = selected_place_ids()
    assert after_ids[:n_before] == orig_ids and len(after_ids) == n_before + take, \
        "selection.yaml 기존 순서가 변형됨 — 중단"

    with CAND_PLACE.open("a", encoding="utf-8") as fh:
        for r in picked:
            fh.write(json.dumps({
                "id": r["id"], "name": r["name"], "region": r["region"],
                "score": 6, "score_parts": {"gallery_3plus": 2, "overview_300plus": 2,
                                            "usetime": int(r["usetime"]), "kogl1_3plus": 1},
                "overview_chars": r["overview_chars"], "gallery": r["gallery"],
                "kogl1": r["kogl1"], "cat3": r.get("cat3"), "base": r["base"],
            }, ensure_ascii=False) + "\n")

    from scripts.common.identifiers import make_demo_gln
    ents_path = WORK_DIR / "expansion" / f"{batch}-entities.txt"
    prev = ents_path.read_text(encoding="utf-8") if ents_path.exists() else ""
    new_lines = [f"414/{make_demo_gln(n_before + i)}" for i in range(1, take + 1)]
    ents_path.write_text(prev + "".join(l + "\n" for l in new_lines), encoding="utf-8")

    regions = Counter(r["region"] for r in picked)
    print(f"  지역 분포: {dict(regions)}")
    print(f"  -> selection.yaml (장소 {n_before}→{n_before + take}) / {ents_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["sweep", "eval", "select", "all"])
    ap.add_argument("--budget", type=int, default=700, help="오늘 쓸 실 호출 수 (일 한도 1,000 중)")
    ap.add_argument("--batch", default="day-03")
    a = ap.parse_args()
    ensure_dirs()
    if a.command in ("sweep", "all"):
        run_sweep()
    if a.command in ("eval", "all"):
        run_eval(a.budget)
    if a.command in ("select", "all"):
        run_select(a.batch)


if __name__ == "__main__":
    main()
