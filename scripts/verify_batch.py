"""Whole-batch checks covering the blind spots of validate.py.

    python -m scripts.verify_batch

1. Neither tree holds a folder that entity-map does not know about.
2. Pages that were not overridden are byte-identical across the two trees.
3. Pages that were overridden actually differ between them.
4. Every media entry in the manifest exists on disk.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.common.config import BENCH_ROOT, WORK_DIR

errors: list[str] = []


def err(m: str) -> None:
    errors.append(m)


def main() -> None:
    entity_map = json.loads((WORK_DIR / "entity-map.json").read_text(encoding="utf-8"))
    manifest = json.loads((BENCH_ROOT / "manifest.json").read_text(encoding="utf-8"))
    slugs = {e.replace("/", "-") for e in entity_map}
    cf_root = BENCH_ROOT / "counterfactual"

    # 1. leftover folders
    for root, label in ((BENCH_ROOT / "entities", "원본"), (cf_root / "entities", "변조본")):
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if d.is_dir() and d.name not in slugs:
                err(f"[1 잔재] {label} 트리에 정체불명 폴더: {d.name}")

    # 2+3. counterfactual tree: identical except where overridden
    ov_path = WORK_DIR / "counterfactual-overrides.jsonl"
    overridden_entities: dict[str, set[str]] = {}
    if ov_path.exists():
        placement = manifest["placement"]
        for line in ov_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ov = json.loads(line)
            page = placement.get(ov["fact_id"])
            if page:
                ent = "-".join(Path(page).parts[1].split("-"))
                overridden_entities.setdefault(Path(page).parts[1], set()).add(Path(page).name)
    n_same = n_diff = 0
    if cf_root.is_dir():
        for entity in entity_map:
            slug = entity.replace("/", "-")
            opages = sorted((BENCH_ROOT / "entities" / slug / "pages").glob("*.html"))
            for op in opages:
                cp = cf_root / "entities" / slug / "pages" / op.name
                if not cp.exists():
                    err(f"[2 동형] {slug}/{op.name}: 변조본에 페이지 없음")
                    continue
                same = op.read_bytes() == cp.read_bytes()
                is_target = op.name in overridden_entities.get(slug, set())
                if is_target and same:
                    err(f"[3 발산] {slug}/{op.name}: 변조 대상인데 두 트리가 동일")
                elif not is_target and not same:
                    err(f"[2 동형] {slug}/{op.name}: 변조 대상이 아닌데 두 트리가 다름")
                n_same += same
                n_diff += not same

    # 4. every manifest media entry exists on disk
    for entity, meta in manifest["entities"].items():
        slug = entity.replace("/", "-")
        for m in meta.get("media", []):
            if not (BENCH_ROOT / "entities" / slug / "media" / m["file"]).exists():
                err(f"[4 media] {slug}: {m['file']} 파일 없음")

    if errors:
        print(f"verify_batch: {len(errors)} FAILURE(S)")
        for e in errors:
            print(" ", e)
        sys.exit(1)
    print(f"verify_batch: OK (엔티티 {len(entity_map)}, 페이지 동일 {n_same}/상이 {n_diff})")


if __name__ == "__main__":
    main()
