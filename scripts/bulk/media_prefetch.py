"""Overlapping image downloader — pull media while the selection rounds run.

    python -m scripts.bulk.media_prefetch

Writes to the same paths the builder expects (entities/01-*/media/), so by the
time a checkpoint finalize starts, the download step has nothing left to do.
Idempotent, 8 at a time.

Two things to know:
  * Facts have not been extracted yet, so the build gate cannot be evaluated —
    this fetches images for entities that will later fail the gate too (1-3%
    waste). When you drop a failed entity from selection.yaml, delete its folder.
  * Do not run alongside finalize: the builder prefetches the same files.
    Running alongside a selection round is safe; they touch different files.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from scripts.common.config import BENCH_ROOT, WORK_DIR
from scripts.common.fetch import get_bytes
from scripts.common.media import to_jpeg
from scripts.domains import FOOD

MAX_CONCURRENCY = 8


def missing_items() -> list[tuple[Path, str]]:
    sel = yaml.safe_load((WORK_DIR / "selection.yaml").read_text(encoding="utf-8"))
    items: list[tuple[Path, str]] = []
    for code in sel.get("food") or []:
        code = str(code)
        media_dir = BENCH_ROOT / "entities" / FOOD.entity_of(code).replace("/", "-") / "media"
        for fname, url, _license in FOOD.media(code):
            target = media_dir / fname
            if not target.exists():
                items.append((target, url))
    return items


def _fetch(item: tuple[Path, str]) -> bool:
    target, url = item
    try:
        data = to_jpeg(get_bytes(url, image=True))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return True
    except Exception:  # noqa: BLE001 — dead images are retried and judged by the build
        return False


def main() -> None:
    items = missing_items()
    print(f"missing images: {len(items)}", flush=True)
    if not items:
        return
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        ok = sum(1 for r in pool.map(_fetch, items) if r)
    print(f"fetched {ok} / failed {len(items) - ok} (failures are retried by finalize)")


if __name__ == "__main__":
    main()
