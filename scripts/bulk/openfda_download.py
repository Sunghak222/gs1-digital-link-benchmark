"""openFDA 벌크 덤프 수확기 — 무엇을 받을지는 docs/18 §8.0, 필드 상세는 docs/19.

    python -m scripts.bulk.openfda_download plan       # 뭘 받을지·용량부터 확인 (네트워크 최소)
    python -m scripts.bulk.openfda_download download   # 실제로 받기 (이어받기·동시 4)
    python -m scripts.bulk.openfda_download verify     # 받은 zip이 성한지 확인 (가끔만)

    # 일부만:  --only ndc,nsde        # 다시 받기: --force

왜 API가 아니라 벌크인가: openFDA는 `skip`이 25,000에서 막혀 전량 수확이 API로는
불가능하다. 게다가 벌크는 한 번 받아두면 재실행이 네트워크 없이 결정적으로 돌아간다
(OFF 덤프와 같은 방식).

docs/14의 교훈을 그대로 적용했다:
  * 패턴 B(안 바뀐 건 다시 안 함) — 이미 받은 파일은 크기를 대조해 건너뛴다.
    기존 라벨 덤프 1.8GB를 재다운로드하지 않는 것이 이 스크립트의 최대 절약이다.
  * 네트워크 기다림은 겹친다 — 동시 4개. 파일이 수백 MB라 8개는 과하고,
    download.open.fda.gov는 공공 서버라 예의도 필요하다.
  * 죽은 주소는 즉시 포기 — 공용 fetch 층이 이미 그렇게 한다(404는 재시도 안 함).
"""
from __future__ import annotations

import argparse
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.common.config import DATA_DIR, WORK_DIR, ensure_dirs
from scripts.common.fetch import cached_json, stream_to_file

MANIFEST_URL = "https://api.fda.gov/download.json"
MANIFEST_CACHE = DATA_DIR / "raw" / "openfda" / "download-manifest.json"
DUMP_ROOT = DATA_DIR / "dump"

#: 받을 것 — 결정 근거는 docs/18 §8.0. (분야, 데이터셋): 왜 받는가
DATASETS: dict[tuple[str, str], str] = {
    ("drug", "label"):       "라벨 본문·Drug Facts·set_id — 주력",
    ("drug", "ndc"):         "함량·포장 계층·식별자 (라벨보다 충실: UNII 80%/RxCUI 60%)",
    ("other", "nsde"):       "NDC 10↔11 변환 공식 정답표 (자체 구현 불가)",
    ("other", "substance"):  "GSRS 허브 — UNII 하나로 ChEBI·MeSH·RxCUI·CAS·ATC",
    ("drug", "drugsfda"):    "승인 이력 + 라벨 개정 PDF 링크 (시간축 보강)",
    ("drug", "enforcement"): "리콜 — gs1:recallStatus와 직결",
    ("drug", "orangebook"):  "제네릭↔오리지널 관계, TE 코드",
    ("other", "unii"):       "UNII↔이름 사전",
    ("drug", "shortages"):   "공급 부족",
}

#: 일부러 안 받는 것 — plan에 이유와 함께 보여준다 (나중에 "왜 없지?"를 막기 위해)
SKIPPED: dict[tuple[str, str], str] = {
    ("drug", "event"):  "FAERS: MedDRA 재배포 금지 + 111GB + 인과관계 없음 (docs/18 §8.0.1)",
    ("device", "udi"):  "의약품이 아님 — GTIN 설계 참고용으로만",
}

#: 이미 받아둔 라벨 덤프가 여기 있다. 같은 폴더를 가리켜야 1.8GB를 다시 받지 않는다.
LEGACY_DIRS: dict[tuple[str, str], str] = {("drug", "label"): "openfda-label"}

#: 크기 비교 허용 오차. 매니페스트의 size_mb는 소수 둘째 자리까지라 1%면 충분하다.
SIZE_TOLERANCE = 0.01


def dest_dir(category: str, dataset: str) -> Path:
    return DUMP_ROOT / LEGACY_DIRS.get((category, dataset), f"openfda-{dataset}")


class Part:
    """받을 파일 한 개. 매니페스트 한 줄 + 우리가 저장할 위치."""

    def __init__(self, category: str, dataset: str, row: dict) -> None:
        self.category, self.dataset = category, dataset
        self.url: str = row["file"]
        self.records: int = row.get("records") or 0
        self.expected_bytes = int(float(row.get("size_mb") or 0) * 1024 * 1024)
        self.path = dest_dir(category, dataset) / self.url.rsplit("/", 1)[-1]

    @property
    def name(self) -> str:
        return f"{self.category}/{self.dataset}"

    def status(self) -> str:
        """받아야 하나? — 'new'(없음) / 'ok'(크기 일치) / 'differs'(있는데 다름)"""
        if not self.path.exists():
            return "new"
        actual = self.path.stat().st_size
        if not self.expected_bytes:
            return "ok"                       # 매니페스트에 크기가 없으면 존재만으로 인정
        gap = abs(actual - self.expected_bytes) / self.expected_bytes
        return "ok" if gap <= SIZE_TOLERANCE else "differs"


def load_parts(only: set[str] | None) -> tuple[list[Part], dict]:
    """매니페스트를 1회 받아(디스크 캐시) 받을 파일 목록으로 편다."""
    manifest = cached_json(MANIFEST_CACHE, MANIFEST_URL)
    results = manifest.get("results") or {}
    parts: list[Part] = []
    meta: dict[tuple[str, str], dict] = {}
    for (category, dataset), _why in DATASETS.items():
        if only and dataset not in only:
            continue
        node = (results.get(category) or {}).get(dataset)
        if not node:
            print(f"  ! 매니페스트에 {category}/{dataset} 없음 — 건너뜀")
            continue
        meta[(category, dataset)] = {
            "export_date": node.get("export_date"),
            "total_records": node.get("total_records"),
        }
        for row in node.get("partitions") or []:
            parts.append(Part(category, dataset, row))
    return parts, meta


def _mb(n: int) -> str:
    mb = n / 1024 / 1024
    return f"{mb:,.1f} MB" if mb < 10 else f"{mb:,.0f} MB"


def cmd_plan(only: set[str] | None) -> None:
    parts, meta = load_parts(only)
    by_dataset: dict[tuple[str, str], list[Part]] = {}
    for p in parts:
        by_dataset.setdefault((p.category, p.dataset), []).append(p)

    todo_bytes = have_bytes = 0
    print(f"{'데이터셋':<22}{'파티션':>7}{'레코드':>12}{'용량':>11}{'상태':>22}")
    print("─" * 76)
    for key, group in by_dataset.items():
        size = sum(p.expected_bytes for p in group)
        recs = meta.get(key, {}).get("total_records") or sum(p.records for p in group)
        counts = {"new": 0, "ok": 0, "differs": 0}
        for p in group:
            counts[p.status()] += 1
        todo_bytes += sum(p.expected_bytes for p in group if p.status() != "ok")
        have_bytes += sum(p.expected_bytes for p in group if p.status() == "ok")
        state = "받아야 함" if counts["new"] else "이미 있음"
        if counts["differs"]:
            state = f"{counts['differs']}개 크기 다름(구버전?)"
        print(f"{'/'.join(key):<22}{len(group):>7}{recs:>12,}{_mb(size):>11}{state:>22}")

    print("─" * 76)
    print(f"받을 용량 {_mb(todo_bytes)}  /  이미 보유 {_mb(have_bytes)}")
    if any(p.status() == "differs" for p in parts):
        print("\n※ '크기 다름'은 로컬이 더 오래된 export일 때 나온다. 갱신하려면 --force.")
    print("\n일부러 안 받는 것:")
    for (cat, ds), why in SKIPPED.items():
        print(f"  · {cat}/{ds:<12} {why}")
    print(f"\n저장 위치: {DUMP_ROOT}")


def cmd_download(only: set[str] | None, force: bool, workers: int) -> None:
    ensure_dirs()
    parts, meta = load_parts(only)
    todo = [p for p in parts if force or p.status() != "ok"]
    if not todo:
        print("받을 것 없음 — 전부 최신이다.")
        return

    total = sum(p.expected_bytes for p in todo)
    print(f"{len(todo)}개 파일 / 약 {_mb(total)} — 동시 {workers}개로 받는다\n")
    done = {"n": 0, "bytes": 0}
    failed: list[tuple[Part, str]] = []

    def fetch(p: Part) -> None:
        try:
            size = stream_to_file(p.url, p.path)
        except Exception as exc:                          # noqa: BLE001 — 한 파일 실패가 전체를 죽이지 않는다
            failed.append((p, repr(exc)))
            print(f"  ✗ {p.name} {p.path.name}: {exc}", flush=True)
            return
        done["n"] += 1
        done["bytes"] += size
        print(f"  ✓ [{done['n']}/{len(todo)}] {p.name:<18} {p.path.name}  {_mb(size)}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fetch, todo))

    print(f"\n완료 {done['n']}/{len(todo)} ({_mb(done['bytes'])})")
    for key, m in meta.items():
        print(f"  {'/'.join(key):<22} export {m['export_date']}  {m['total_records']:,}건")
    if failed:
        print(f"\n실패 {len(failed)}건 — 같은 명령을 다시 실행하면 실패분만 이어받는다:")
        for p, err in failed:
            print(f"  · {p.path.name}: {err}")
        raise SystemExit(1)


def cmd_verify(only: set[str] | None) -> None:
    """zip이 열리는지 + 안에 든 레코드 수가 매니페스트와 맞는지.

    상시 경로에는 넣지 않는다 — docs/14 패턴 C(일회성 점검이 상시 업무로 눌러앉는 것)를
    피하려고 별도 명령으로 뺐다. 다운로드 직후·의심될 때만 돌린다.
    """
    parts, _ = load_parts(only)
    bad = 0
    for p in parts:
        if not p.path.exists():
            print(f"  - {p.path.name}: 없음")
            continue
        try:
            with zipfile.ZipFile(p.path) as z:
                inner = [n for n in z.namelist() if n.endswith(".json")]
                with z.open(inner[0]) as fh:
                    n = len(json.load(fh).get("results") or [])
        except Exception as exc:                          # noqa: BLE001
            print(f"  ✗ {p.path.name}: 열리지 않음 — {exc}")
            bad += 1
            continue
        mark = "✓" if (not p.records or n == p.records) else "≠"
        if mark == "≠":
            bad += 1
        print(f"  {mark} {p.path.name:<44} {n:>9,}건 (매니페스트 {p.records:,})")
    print(f"\n이상 {bad}건")
    if bad:
        raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "download", "verify"])
    ap.add_argument("--only", default=None,
                    help="데이터셋 이름 쉼표 구분 (예: ndc,nsde). 생략하면 전부")
    ap.add_argument("--force", action="store_true", help="이미 있어도 다시 받는다")
    ap.add_argument("--workers", type=int, default=4, help="동시 다운로드 수 (기본 4)")
    a = ap.parse_args()

    only = {s.strip() for s in a.only.split(",")} if a.only else None
    if a.command == "plan":
        cmd_plan(only)
    elif a.command == "download":
        cmd_download(only, a.force, a.workers)
    else:
        cmd_verify(only)


if __name__ == "__main__":
    main()
