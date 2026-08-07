"""오버랩 이미지 다운로더 — 선별 라운드가 도는 동안 이미지를 미리 받아둔다 (2026-08-05, docs/12·13).

    python -m scripts.bulk.media_prefetch

selection.yaml의 식품 엔티티 중 미보유 이미지를 build_fixtures.collect_media와
같은 파일명·경로(entities/01-*/media/)로 저장한다(동시 8, 멱등). 빌드는 파일이
있으면 그대로 쓰므로 체크포인트 finalize에서 다운로드 시간이 사라진다.

주의:
- 팩트 추출 전이라 게이트 판정이 불가능 → 나중에 게이트 탈락할 엔티티의 이미지도
  받는다(~1~3% 낭비). 탈락 처리 시 selection 제거와 함께 폴더도 지울 것(잔재 방지).
- finalize와 동시 실행 금지 — 빌드의 프리페치와 같은 파일을 동시에 쓸 수 있다.
  (선별 select와의 동시 실행은 안전: 서로 다른 파일만 만짐)
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from scripts.build_fixtures import _to_jpeg, collect_media
from scripts.common.config import BENCH_ROOT, WORK_DIR
from scripts.common.fetch import get_bytes
from scripts.common.identifiers import gtin_to_14


def missing_items() -> list[tuple[Path, str]]:
    sel = yaml.safe_load((WORK_DIR / "selection.yaml").read_text(encoding="utf-8"))
    items = []
    for code in sel.get("food") or []:
        code = str(code)
        media_dir = BENCH_ROOT / "entities" / f"01-{gtin_to_14(code)}" / "media"
        for fname, url, _lic in collect_media("food", code):
            target = media_dir / fname
            if not target.exists():
                items.append((target, url))
    return items


def _fetch(item: tuple[Path, str]) -> bool:
    target, url = item
    try:
        data = _to_jpeg(get_bytes(url, image=True))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return True
    except Exception:  # noqa: BLE001 — 죽은 이미지 등은 빌드가 재시도 후 판정
        return False


def main() -> None:
    items = missing_items()
    print(f"미보유 이미지 {len(items)}건", flush=True)
    if not items:
        return
    with ThreadPoolExecutor(max_workers=8) as ex:
        ok = sum(1 for r in ex.map(_fetch, items) if r)
    print(f"받음 {ok} / 실패 {len(items) - ok} (실패분은 finalize가 재시도 후 판정)")


if __name__ == "__main__":
    main()
