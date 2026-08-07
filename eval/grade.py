"""Grade a run from its records alone — the pipeline is never re-run.

    python eval/grade.py results/<run_dir>
    python eval/grade.py results/<run_dir> --review-sample 10   # + human review sheet

Judged against qa.jsonl, facts.jsonl and the manifest. The judge is a different
model from the one under test, which keeps it cheap and avoids self-grading bias.
Writes <run_dir>/grades.jsonl and, on request, <run_dir>/review-sample.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapter  # noqa: E402

BENCH_ROOT = adapter.BENCH_ROOT

JUDGE_SYSTEM = """You grade answers from a question-answering system against an expected answer.
Given QUESTION, EXPECTED_ANSWER, EXPECTED_VALUES (the source-of-truth values), and SYSTEM_ANSWER, return a verdict:

- "correct": the system answer conveys the expected value(s). Allow rounding, unit formatting
  differences (22.8 g vs 22.8g), and answering in a different language than the expected answer —
  judge meaning, not wording. Grade ONLY whether the asked value is conveyed: additional
  information beyond the expected answer does NOT lower the verdict unless it contradicts the
  expected value (e.g. expected "allergen: peanuts" + answer also mentioning may-contain traces
  is still "correct").
- "partial": some of the asked values are right, others missing or wrong (a missing expected
  value is "partial"; extra information is not).
- "incorrect": the answer contradicts the expected value(s) or answers something else.
- "no_answer": the answer is a progress/deferral message ("I am checking additional sources...")
  or says it does not know.

Return ONLY JSON: {"verdict": "...", "reason": "<one short sentence>"}"""


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def fact_value_str(fact: dict[str, Any]) -> str:
    v = fact.get("value")
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)


def page_hit(record: dict[str, Any], gold_pages: list[str]) -> bool | None:
    """Did the pages the pipeline read include the gold fact's page? None if undecidable."""
    read = record.get("retrieval", {}).get("read_pages") or []
    if not gold_pages:
        return None
    for gp in gold_pages:
        tail = "/".join(gp.split("/")[-2:])          # "pages/x.html" | "media/y.jpg"
        if any(tail in str(r) for r in read):
            return True
    return False


async def judge_one(client, model: str, qa: dict[str, Any], answer: str, facts: dict[str, dict]) -> dict[str, Any]:
    values = {fid: fact_value_str(facts[fid]) for fid in qa["gold_fact_ids"] if fid in facts}
    user = (
        f"QUESTION: {qa['question']}\n"
        f"EXPECTED_ANSWER: {qa['gold_answer']}\n"
        f"EXPECTED_VALUES: {json.dumps(values, ensure_ascii=False)}\n"
        f"SYSTEM_ANSWER: {answer}"
    )
    resp = await client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
    )
    out = json.loads(resp.choices[0].message.content or "{}")
    usage = resp.usage
    return {
        "verdict": out.get("verdict", "parse_error"),
        "judge_reason": out.get("reason", ""),
        "judge_tokens": {"input": usage.prompt_tokens, "output": usage.completion_tokens},
    }


def quadrant(verdict: str, hit: bool | None) -> str:
    ok = verdict == "correct"
    if hit is None:
        return "대조불가"
    if hit and ok:
        return "읽음·맞음(정상)"
    if hit and not ok:
        return "읽음·틀림(읽기/합성 실패)"
    if not hit and not ok:
        return "못읽음·틀림(검색 실패)"
    return "못읽음·맞음(요주의: 사전지식 의심)"


def write_review_sample(run_dir: Path, graded: list[dict[str, Any]], qa_by_id: dict, n: int) -> Path:
    """Review sheet: 60% not-correct verdicts, 40% correct ones, so both directions get checked."""
    rng = random.Random(42)
    flagged = [g for g in graded if g["verdict"] != "correct"]
    normal = [g for g in graded if g["verdict"] == "correct"]
    rng.shuffle(flagged)
    rng.shuffle(normal)
    k = min(len(flagged), max(1, round(n * 0.6)))
    sample = flagged[:k] + normal[: n - k]
    lines = [
        "# 채점 검수표 — 판정이 맞으면 ○, 틀리면 ✗를 '검수' 칸에",
        "",
        f"run: `{run_dir.name}` · 채점 모델 판정 {n}건 (correct 아닌 판정 우선 포함)",
        "",
        "| # | 질문 | 정답 | 시스템 답변 | 판정 | 판정 근거 | 검수 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, g in enumerate(sample, 1):
        qa = qa_by_id[g["qa_id"]]
        row = [str(i), qa["question"], qa["gold_answer"], str(g["answer"]), g["verdict"], g["judge_reason"], " "]
        lines.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in row) + " |")
    out = run_dir / "review-sample.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


async def run(args: argparse.Namespace) -> None:
    adapter.setup_core_env()
    from openai import AsyncOpenAI
    from packages.agents.utils.config import settings

    run_dir = Path(args.run_dir)
    records = load_jsonl(run_dir / "results.jsonl")
    qa_by_id = {q["qa_id"]: q for q in load_jsonl(BENCH_ROOT / "qa.jsonl")}
    facts = {f["fact_id"]: f for f in load_jsonl(BENCH_ROOT / "facts.jsonl")}
    placement = json.loads((BENCH_ROOT / "manifest.json").read_text(encoding="utf-8"))["placement"]

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    sem = asyncio.Semaphore(args.concurrency)

    async def grade_record(r: dict[str, Any]) -> dict[str, Any]:
        qa = qa_by_id[r["qa_id"]]
        async with sem:
            j = await judge_one(client, args.model, qa, str(r.get("answer")), facts)
        gold_pages = [placement[fid] for fid in qa["gold_fact_ids"] if fid in placement]
        hit = page_hit(r, gold_pages)
        return {
            "qa_id": r["qa_id"], "answer": r.get("answer"),
            **j,
            "answer_page_hit": hit, "gold_pages": gold_pages,
            "quadrant": quadrant(j["verdict"], hit),
            "tags": qa["tags"], "judge_model": args.model,
        }

    graded = await asyncio.gather(*[grade_record(r) for r in records])
    with (run_dir / "grades.jsonl").open("w", encoding="utf-8") as fh:
        for g in graded:
            fh.write(json.dumps(g, ensure_ascii=False) + "\n")

    verdicts = Counter(g["verdict"] for g in graded)
    quads = Counter(g["quadrant"] for g in graded)
    ji = sum(g["judge_tokens"]["input"] for g in graded)
    jo = sum(g["judge_tokens"]["output"] for g in graded)
    print(f"채점 완료: {len(graded)}문항 → {run_dir / 'grades.jsonl'}")
    print("판정:", dict(verdicts))
    print("4분면:", dict(quads))
    print(f"채점 토큰: 입력 {ji/1e3:.0f}k / 출력 {jo/1e3:.0f}k (모델 {args.model})")

    if args.review_sample:
        out = write_review_sample(run_dir, list(graded), qa_by_id, args.review_sample)
        print(f"검수표: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--review-sample", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
