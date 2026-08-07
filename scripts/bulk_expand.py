"""대량 확장 통합 실행기 (docs/08 §8) — 매일 이것만 실행하면 된다.

    python -m scripts.bulk_expand food --target 200 --batch day-03    # 식품 수확+선별
    python -m scripts.bulk_expand place --budget 700 --batch day-03   # 장소 수확+선별 (한도 인지)
    python -m scripts.bulk_expand facts                               # 팩트 추출만 (라운드마다)
    python -m scripts.bulk_expand finalize --batch day-03             # 추출→빌드×2→검증→검수 리포트

food/place는 독립적으로 하루 몇 번이든 안전(캐시·이어달리기).
2026-08-04 사용자 결정: 라운드마다는 facts까지만 진행하고, 빌드·검증은 코퍼스 마감 시
finalize 1회로 몰아서(탈락자는 그때 제외). 빌드는 엔티티 단위 오류 격리됨(BUILD FAIL 보고).
권장: 2~3라운드마다 finalize를 체크포인트로 한 번씩 돌려 문제가 쌓이는 것을 방지.
"""
from __future__ import annotations

import argparse
import subprocess
import sys


def run(mod: str, *args: str) -> None:
    cmd = [sys.executable, "-m", mod, *args]
    print(f"\n$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["food", "place", "facts", "finalize"])
    ap.add_argument("--target", type=int, default=200)
    ap.add_argument("--budget", type=int, default=700)
    ap.add_argument("--batch", default=None)
    a = ap.parse_args()
    if a.command != "facts" and not a.batch:
        ap.error("--batch is required for food/place/finalize")

    if a.command == "food":
        run("scripts.bulk.off_harvest", "all", "--target", str(a.target), "--batch", a.batch)
    elif a.command == "place":
        run("scripts.bulk.tour_harvest", "all", "--budget", str(a.budget), "--batch", a.batch)
    elif a.command == "facts":
        run("scripts.extract_facts")
        print("\nfacts 완료 — 빌드·검증·검수는 코퍼스 마감(또는 체크포인트) 때 finalize로 실행")
    else:
        # 2026-07-29 사용자 결정: VLM 영양라벨 검증은 일일 배치에서 제외 —
        # 결과 소비자가 gen_qa뿐이라 QA 마일스톤 직전 전체 1회로 미룬다.
        run("scripts.extract_facts")
        run("scripts.build_fixtures")
        run("scripts.build_fixtures", "--overrides", "work/counterfactual-overrides.jsonl")
        run("scripts.validate")
        run("scripts.verify_batch")
        run("scripts.bulk.make_review", "--batch", a.batch)
        print("\nfinalize 완료 — work/expansion/<batch>/entity-review.md 확인")


if __name__ == "__main__":
    main()
