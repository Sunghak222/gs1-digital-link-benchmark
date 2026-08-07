"""Doc 04 §3 — QA 루프. 문항마다 실파이프라인을 돌리고, 기록만 저장한다.

    python eval/run_eval.py --qa-ids eval/pilot.txt                  # 전체 그래프 (원본, miss)
    python eval/run_eval.py --fast-miss --qa-ids ...                 # 문서 경로만 (doc 05 §1)
    python eval/run_eval.py --corpus counterfactual --qa-ids ...     # 변조본 run

miss 실행기는 두 가지다:
  full — 그래프 전체(KG 헛걸음 포함). critic_1의 조기종료 진단이 필요한 표본용.
  fast — 문서 경로(external→수거→합성→critic_2)만. miss에서 KG 헛걸음 구간의 결말은
         정해져 있으므로(빈 KG→진행형 답→external행) 실행하지 않고, 그 구간의 시간은
         full 표본에서 상수로 측정해 TTFA에 보정한다. critic_2 이후의 재시도 라우팅은
         코어의 라우팅 함수를 그대로 사용해 전체 그래프와 동일하게 돈다.

타이머·토큰 계측은 코어 metrics.py(usage_metrics)를 그대로 재사용 — 여기서 새로 재지 않는다.
채점은 별도 패스(grade.py, E2). 이 스크립트는 실행 기록(results.jsonl)까지만 책임진다.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapter  # noqa: E402

BENCH_ROOT = adapter.BENCH_ROOT
QA_PATH = BENCH_ROOT / "qa.jsonl"
OVERRIDES_PATH = BENCH_ROOT / "work" / "counterfactual-overrides.jsonl"


# ---------------------------------------------------------------------------
# 입력
# ---------------------------------------------------------------------------

def load_qa(qa_ids: list[str] | None) -> list[dict[str, Any]]:
    items = [json.loads(line) for line in QA_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if qa_ids is not None:
        wanted = set(qa_ids)
        items = [q for q in items if q["qa_id"] in wanted]
        missing = wanted - {q["qa_id"] for q in items}
        if missing:
            raise SystemExit(f"qa_id not found in qa.jsonl: {sorted(missing)}")
    return items


def group_by_entity(items: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for qa in items:
        groups.setdefault(qa["entity"], []).append(qa)
    return list(groups.items())


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 실행기 — 문항 하나를 최종 state로
# ---------------------------------------------------------------------------

async def ask_full(pipeline, question: str, dl_path: str, recursion_limit: int) -> dict[str, Any]:
    return await pipeline.graph.ainvoke(
        pipeline._initial_state(question, dl_path),
        config={"recursion_limit": recursion_limit},
    )


#: critic_2 라우팅이 가리키는 노드 이름 → 거기서부터 critic_2까지의 구간 (그래프 edge와 동일)
_DOC_PATH_SEGMENTS = {
    "external_retrieval": ("external_retrieval", "collect_vector_result", "synthesize_answer_2", "critic_2"),
    "retrieve_kg_2": ("retrieve_kg_2", "synthesize_answer_2", "critic_2"),
    "synthesize_answer_2": ("synthesize_answer_2", "critic_2"),
}


async def ask_fast_miss(pipeline, question: str, dl_path: str, recursion_limit: int) -> dict[str, Any]:
    from packages.agents.utils.metrics import call_observed_node

    nodes = {
        "external_retrieval": pipeline.external_retriever,
        "collect_vector_result": pipeline.vector_result_collector,
        "retrieve_kg_2": pipeline.kg_retriever,
        "synthesize_answer_2": pipeline.answer_synthesizer,
        "critic_2": pipeline.critic_2,
    }
    state = pipeline._initial_state(question, dl_path)
    segment = _DOC_PATH_SEGMENTS["external_retrieval"]
    for _ in range(recursion_limit):
        for name in segment:
            state = {**state, **await call_observed_node(name, nodes[name], state)}
        nxt = pipeline._route_after_critic_2(state)   # 재시도 판단은 코어 라우팅 그대로
        if nxt == "end":
            return state
        segment = _DOC_PATH_SEGMENTS[nxt]
    raise RuntimeError(f"recursion_limit({recursion_limit}) exceeded in fast-miss loop")


# ---------------------------------------------------------------------------
# 기록 추출 — 최종 state(= FastAPI ChatResponse에 노출되는 그 필드들)에서 꺼낸다
# ---------------------------------------------------------------------------

def _read_pages(state: dict[str, Any]) -> list[str]:
    pages: list[str] = []
    dl = state.get("digital_link_result") or {}
    for item in dl.get("traversed_links") or []:
        args = item.get("arguments") or {}
        if args.get("path"):
            pages.append(str(args["path"]))
    for item in dl.get("image_analyses") or []:
        img = str(item.get("image") or "")
        pages.append(img[:80] + "..." if img.startswith("data:") else img)
    vec = state.get("vector_result") or {}
    for item in vec.get("items") or []:
        payload = item.get("payload") if isinstance(item, dict) else None
        src = (payload or {}).get("source_url") if isinstance(payload, dict) else None
        if src:
            pages.append(str(src))
    return pages


def extract_record(qa: dict[str, Any], state: dict[str, Any], metrics: dict[str, Any], runner: str) -> dict[str, Any]:
    nodes = metrics.get("nodes") or []
    node_seq = [n.get("node") for n in nodes]
    external_used = "external_retrieval" in node_seq
    totals = metrics.get("totals") or {}
    return {
        "qa_id": qa["qa_id"],
        "entity": qa["entity"],
        "question": qa["question"],
        "answer": state.get("answer"),
        "path": {
            "runner": runner,
            "node_seq": node_seq,
            "external_used": external_used,
            # miss에서 external 없이 끝남 = 조기종료(§4.4). full 실행에서만 관측 가능.
            "early_exit": (not external_used) if runner == "full" else None,
            "critic_stage": state.get("critic_stage"),
            "critic_flags": {
                "go_to_kg": state.get("go_to_kg"),
                "external_retrieval": state.get("external_retrieval"),
                "go_to_generate": state.get("go_to_generate"),
            },
            "failure_reason": state.get("failure_reason"),
            "retries": {
                "kg": state.get("retrieval_attempts"),
                "external": state.get("external_retrieval_attempts"),
                "generate": state.get("generation_attempts"),
            },
        },
        "retrieval": {
            "read_pages": _read_pages(state),
            "used_sources": state.get("used_sources"),
        },
        "latency": {
            "total_s": round((metrics.get("pipeline_elapsed_ms") or 0) / 1000, 3),
            "nodes": {n.get("node"): round((n.get("latency_ms") or 0) / 1000, 3) for n in nodes},
        },
        "tokens": {
            "input": totals.get("input_tokens"),
            "output": totals.get("output_tokens"),
            "llm_calls": totals.get("llm_calls"),
        },
    }


# ---------------------------------------------------------------------------
# 실행 루프 (doc 04 §3)
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> None:
    corpus_root = BENCH_ROOT / ("entities" if args.corpus == "original" else "counterfactual")
    pipeline, _ = adapter.build_pipeline(corpus_root)

    from packages.agents.utils.metrics import finalize_usage_metrics
    from packages.vector_index.retrievers import vector

    if not vector.is_configured():
        raise SystemExit("vector store not configured — Neo4j 설정(.env)과 기동 상태를 확인하세요.")

    qa_ids = None
    if args.qa_ids:
        p = Path(args.qa_ids)
        raw = p.read_text(encoding="utf-8").split() if p.exists() else args.qa_ids.split(",")
        qa_ids = [x.strip() for x in raw if x.strip()]
    qa_items = load_qa(qa_ids)
    if not qa_items:
        raise SystemExit("no QA selected")

    runner = "fast" if args.fast_miss else "full"
    # 폴더명 = 시각 + 실험 이름. 한 번의 실험 = 폴더 하나 (조건 상세는 run.json에).
    experiment = "hit" if args.kg == "hit" else f"miss-{args.corpus}"
    if args.resume:
        run_dir = Path(args.resume)
        prev = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        if prev.get("kg_state") != args.kg or prev.get("corpus") != args.corpus:
            raise SystemExit(f"재개 대상의 조건이 다릅니다: {prev.get('kg_state')}/{prev.get('corpus')}")
        done = {json.loads(l)["qa_id"] for l in (run_dir / "results.jsonl").open(encoding="utf-8")} \
            if (run_dir / "results.jsonl").exists() else set()
        qa_items = [q for q in qa_items if q["qa_id"] not in done]
        print(f"재개: 완료 {len(done)}문항 건너뜀, 남은 {len(qa_items)}문항")
        if not qa_items:
            raise SystemExit("남은 문항이 없습니다 — 이미 완주된 실험입니다.")
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = BENCH_ROOT / "results" / f"{stamp}_{experiment}"
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "raw").mkdir()

    from packages.agents.utils.config import settings as agent_settings
    from packages.vector_index.config import settings as vec_settings

    if args.resume:
        info = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        info.setdefault("resumes", []).append(
            {"at": datetime.now().isoformat(), "remaining_qa": len(qa_items)})
        (run_dir / "run.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        (run_dir / "run.json").write_text(json.dumps({
            "experiment": experiment,
            "started_at": datetime.now().isoformat(),
            "mode": "scan-session", "kg_state": args.kg, "runner": runner,
            "corpus": args.corpus, "cache": args.cache, "rep": args.rep,
            "corpus_root": str(corpus_root),
            "qa_count": len(qa_items),
            "qa_ids": [q["qa_id"] for q in qa_items],
            "qa_sha256": sha256_of(QA_PATH),
            "overrides_sha256": sha256_of(OVERRIDES_PATH) if OVERRIDES_PATH.exists() else None,
            "models": {
                "chat": agent_settings.openai_model,
                "vlm": vec_settings.vlm_model,
                "embed": f"{vec_settings.embed_backend}:{vec_settings.embed_model}",
            },
            "retrieval_mode": vec_settings.retrieval_mode,
            "vector_backend": vec_settings.vector_backend,
            "recursion_limit": args.recursion_limit,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    results_path = run_dir / "results.jsonl"
    done = 0
    for entity, qas in group_by_entity(qa_items):
        reset = vector.reset()                      # 새 스캔 세션 (doc 04 §2)
        if not reset.get("ok"):
            raise SystemExit(f"vector.reset() failed: {reset}")
        dl_path = "/" + entity
        for qa in qas:
            ask = ask_fast_miss if runner == "fast" else ask_full
            state = await ask(pipeline, qa["question"], dl_path, args.recursion_limit)
            metrics = finalize_usage_metrics(state)
            record = extract_record(qa, state, metrics, runner)
            with results_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            (run_dir / "raw" / f"{qa['qa_id']}.json").write_text(
                json.dumps({**state, "usage_metrics": metrics}, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            done += 1
            print(f"[{done}/{len(qa_items)}] {qa['qa_id']}  "
                  f"total={record['latency']['total_s']}s  external={record['path']['external_used']}  "
                  f"answer={str(record['answer'])[:80]!r}")

    print(f"\nrun dir: {run_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", choices=["original", "counterfactual"], default="original")
    parser.add_argument("--kg", choices=["miss", "hit"], default="miss",
                        help="hit는 E4(ingest_kg.py 이후)에서 지원")
    parser.add_argument("--fast-miss", action="store_true",
                        help="miss를 문서 경로부터 실행 — KG 헛걸음 구간은 full 표본에서 상수로 보정 (doc 05 §1)")
    parser.add_argument("--resume", metavar="RUN_DIR",
                        help="중단된 실험을 같은 폴더에 이어서 실행 (완료 문항은 건너뜀)")
    parser.add_argument("--qa-ids", help="qa_id 목록 파일 경로 또는 콤마 구분 목록")
    parser.add_argument("--cache", choices=["warm", "cold"], default="warm", help="기록용 라벨")
    parser.add_argument("--rep", type=int, default=1)
    parser.add_argument("--recursion-limit", type=int, default=50)
    args = parser.parse_args()
    if args.kg == "hit":
        raise SystemExit("--kg hit는 E4에서 구현됩니다 (ingest_kg.py 필요)")
    if args.fast_miss and args.kg != "miss":
        raise SystemExit("--fast-miss는 miss 실행 전용입니다 (hit은 KG 경로가 측정 대상)")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
