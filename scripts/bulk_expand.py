"""Front end for the bulk expansion loop — the only command a daily run needs.

    python -m scripts.bulk_expand food  --target 200 --batch day-03   # harvest + select
    python -m scripts.bulk_expand place --budget 700 --batch day-03   # quota-aware
    python -m scripts.bulk_expand facts                               # extraction only
    python -m scripts.bulk_expand finalize --batch day-03             # extract, build x2, validate, review

food and place are independent and safe to run repeatedly: both cache and resume.
A round normally stops after `facts`; building and validating are batched into one
`finalize` when the corpus closes, which is also where gate failures are dropped.
Run finalize as a checkpoint every few rounds so problems cannot pile up.
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
        # The VLM nutrition-label check is deliberately absent: gen_qa is its only
        # consumer, so it runs once before a QA milestone rather than every batch.
        run("scripts.extract_facts")
        run("scripts.build_fixtures")
        run("scripts.build_fixtures", "--overrides", "work/counterfactual-overrides.jsonl")
        run("scripts.validate")
        run("scripts.verify_batch")
        run("scripts.bulk.make_review", "--batch", a.batch)
        print("\nfinalize 완료 — work/expansion/<batch>/entity-review.md 확인")


if __name__ == "__main__":
    main()
