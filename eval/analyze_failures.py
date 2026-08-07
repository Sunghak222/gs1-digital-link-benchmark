"""오답 분석 — correct가 아닌 문항들의 실패 지점을 증거 사슬로 분류한다.

    python eval/analyze_failures.py results/<run_dir>

grades.jsonl(채점)과 raw/<qa_id>.json(실행 전 과정)을 대조해 문항마다:
  ① 정답 페이지를 읽었는가 (answer_page_hit)
  ② 정답 값이 LLM 프롬프트(state)까지 도달했는가 (raw 전문에서 값 문자열 탐색 — 휴리스틱)
  ③ 안 읽었다면 후보 선택 단계에서 그 페이지가 어떤 취급을 받았는가
를 확인하고 실패 유형을 붙인다. 결과: <run_dir>/failure-analysis.md
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapter  # noqa: E402

BENCH_ROOT = adapter.BENCH_ROOT


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def value_tokens(value: Any) -> list[str]:
    """raw 전문에서 찾아볼 값 문자열 후보들 (숫자·문자열 단위로 분해)."""
    if isinstance(value, dict):
        return [t for v in value.values() for t in value_tokens(v)]
    if isinstance(value, list):
        return [t for v in value for t in value_tokens(v)]
    s = str(value).strip()
    if isinstance(value, float) and s.endswith(".0"):
        return [s, s[:-2]]
    return [s] if len(s) >= 2 else []


def gold_page_treatment(raw: dict[str, Any], gold_pages: list[str]) -> str:
    """정답 페이지가 후보 선택에서 받은 취급 (traverse/preprocess/skip/후보목록에 없음)."""
    cands = (raw.get("candidate_plan") or {}).get("candidates") or []
    tails = {"/".join(g.split("/")[-2:]) for g in gold_pages}
    actions = [c.get("action") for c in cands
               if any(t in str(c.get("source_url", "")) for t in tails)]
    if actions:
        return "/".join(sorted(set(a or "?" for a in actions)))
    return "후보목록에 없음"


def classify(g: dict[str, Any], evidence_reached: bool, treatment: str, answer: str) -> str:
    hit = g.get("answer_page_hit")
    if hit and evidence_reached:
        return "A. 합성 실패 — 증거가 프롬프트에 도달했는데 답을 못/잘못 냄"
    if hit and not evidence_reached:
        return "B. 추출 유실 — 페이지는 읽었지만 값이 추출 단계에서 빠짐 (예: JSON-LD 우선 추출의 본문 폐기)"
    if not hit and treatment == "skip":
        return "C. 후보 선택 실패 — 선택기가 정답 페이지를 건너뜀"
    if not hit:
        return f"D. 검색/도달 실패 — 정답 페이지 미읽음 (후보 취급: {treatment})"
    return "E. 기타"


def main() -> None:
    run_dir = Path(sys.argv[1])
    grades = [g for g in load_jsonl(run_dir / "grades.jsonl") if g["verdict"] != "correct"]
    qa_by_id = {q["qa_id"]: q for q in load_jsonl(BENCH_ROOT / "qa.jsonl")}
    facts = {f["fact_id"]: f for f in load_jsonl(BENCH_ROOT / "facts.jsonl")}

    rows = []
    for g in grades:
        qa = qa_by_id[g["qa_id"]]
        raw_path = run_dir / "raw" / f"{g['qa_id']}.json"
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        raw_text = json.dumps(raw, ensure_ascii=False)
        tokens = [t for fid in qa["gold_fact_ids"] for t in value_tokens(facts.get(fid, {}).get("value"))]
        evidence = any(t in raw_text for t in tokens) if tokens else False
        treatment = gold_page_treatment(raw, g.get("gold_pages") or [])
        # 채점 규칙 이슈 후보: 답변 안에 정답 값 토큰이 전부 있는데 correct가 아님
        answer = str(g.get("answer") or "")
        all_in_answer = bool(tokens) and all(t in answer for t in tokens)
        category = ("J. 채점 규칙 이슈 의심 — 답변에 정답 값이 전부 있음 (추가 정보 감점?)"
                    if all_in_answer else classify(g, evidence, treatment, answer))
        rows.append({
            "qa_id": g["qa_id"], "verdict": g["verdict"], "category": category,
            "question": qa["question"], "gold": qa["gold_answer"],
            "answer": answer, "reason": g.get("judge_reason", ""),
            "page_hit": g.get("answer_page_hit"), "evidence_reached": evidence,
            "treatment": treatment, "modality": qa["tags"]["modality"], "lang": qa["tags"]["lang"],
        })

    counts = Counter(r["category"] for r in rows)
    lines = [
        "# 오답 분석 — ③-a miss × 원본",
        "",
        f"run `{run_dir.name}` · correct 아님 {len(rows)}문항 "
        "(판정: " + ", ".join(f"{k} {v}" for k, v in Counter(r['verdict'] for r in rows).items()) + ")",
        "",
        "증거 사슬 자동 추적: 정답 페이지 읽음 여부 → 정답 값의 프롬프트 도달 여부(문자열 탐색 휴리스틱",
        "— 단위 표기 차이로 오탐지 가능, 개별 건은 raw 파일로 재확인) → 후보 선택 취급.",
        "",
        "## 유형 요약",
        "",
        "| 유형 | 건수 |",
        "|---|---|",
    ]
    for cat, n in counts.most_common():
        lines.append(f"| {cat} | {n} |")
    lines.append("")

    for cat, _ in counts.most_common():
        lines.append(f"## {cat}")
        lines.append("")
        for r in [r for r in rows if r["category"] == cat]:
            lines += [
                f"### `{r['qa_id']}` — {r['verdict']} ({r['modality']}/{r['lang']})",
                f"- 질문: {r['question']}",
                f"- 정답: {r['gold']}",
                f"- 답변: {r['answer'][:300]}",
                f"- 채점 근거: {r['reason']}",
                f"- 추적: 정답페이지읽음={r['page_hit']} · 값도달={r['evidence_reached']} · 후보취급={r['treatment']}",
                "",
            ]

    out = run_dir / "failure-analysis.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"분석 완료: {len(rows)}건 → {out}")
    for cat, n in counts.most_common():
        print(f"  {n:2d}  {cat}")


if __name__ == "__main__":
    main()
