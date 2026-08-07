"""openFDA bulk dump harvester. What to fetch is decided in docs/18 §8.0;
per-field detail is in docs/19.

    python -m scripts.bulk.openfda_download plan       # what and how much
    python -m scripts.bulk.openfda_download download   # fetch (resumable, 4 at a time)
    python -m scripts.bulk.openfda_download verify     # zip integrity + record counts

    # subset: --only ndc,nsde        # re-fetch: --force

Bulk rather than the API: `skip` caps at 25,000, so a full harvest is impossible
over HTTP, and a local dump makes every later run deterministic and offline.

Following docs/14:
  * skip what is already on disk by comparing sizes — the existing 1.8 GB label
    dump is the single biggest saving here
  * 4 concurrent downloads, not 8: these are hundred-megabyte files and
    download.open.fda.gov is a public server
  * dead URLs fail fast — the shared fetch layer already refuses to retry 404s
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

#: (category, dataset): why we take it. Rationale in docs/18 §8.0.
DATASETS: dict[tuple[str, str], str] = {
    ("drug", "label"):       "label text, Drug Facts sections, set_id — the main source",
    ("drug", "ndc"):         "strength, package hierarchy, richer identifiers than the labels",
    ("other", "nsde"):       "authoritative NDC 10<->11 conversion table",
    ("other", "substance"):  "GSRS hub — one UNII reaches ChEBI, MeSH, RxCUI, CAS, ATC",
    ("drug", "drugsfda"):    "approval history and links to revised label PDFs",
    ("drug", "enforcement"): "recalls",
    ("drug", "orangebook"):  "generic-to-originator relations, TE codes",
    ("other", "unii"):       "UNII to substance-name lookup",
    ("drug", "shortages"):   "supply shortages",
}

#: Deliberately not fetched. Listed so `plan` can answer "why is this missing?".
SKIPPED: dict[tuple[str, str], str] = {
    ("drug", "event"):  "FAERS: MedDRA cannot be redistributed, 111 GB, no causality",
    ("device", "udi"):  "not a drug — kept only as a reference for GTIN modelling",
}

#: The label dump already lives here; pointing at it avoids re-fetching 1.8 GB.
LEGACY_DIRS: dict[tuple[str, str], str] = {("drug", "label"): "openfda-label"}

#: size_mb in the manifest has two decimals, so 1% is a generous tolerance.
SIZE_TOLERANCE = 0.01


def dest_dir(category: str, dataset: str) -> Path:
    return DUMP_ROOT / LEGACY_DIRS.get((category, dataset), f"openfda-{dataset}")


class Part:
    """One downloadable file: a manifest row plus where we keep it."""

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
        """'new' (absent) / 'ok' (size matches) / 'differs' (present but stale)."""
        if not self.path.exists():
            return "new"
        actual = self.path.stat().st_size
        if not self.expected_bytes:
            return "ok"
        gap = abs(actual - self.expected_bytes) / self.expected_bytes
        return "ok" if gap <= SIZE_TOLERANCE else "differs"


def load_parts(only: set[str] | None) -> tuple[list[Part], dict]:
    """Read the manifest once (disk-cached) and flatten it into files to fetch."""
    manifest = cached_json(MANIFEST_CACHE, MANIFEST_URL)
    results = manifest.get("results") or {}
    parts: list[Part] = []
    meta: dict[tuple[str, str], dict] = {}
    for (category, dataset), _why in DATASETS.items():
        if only and dataset not in only:
            continue
        node = (results.get(category) or {}).get(dataset)
        if not node:
            print(f"  ! {category}/{dataset} missing from the manifest — skipped")
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
    print(f"{'dataset':<22}{'parts':>7}{'records':>12}{'size':>11}{'status':>22}")
    print("-" * 76)
    for key, group in by_dataset.items():
        size = sum(p.expected_bytes for p in group)
        recs = meta.get(key, {}).get("total_records") or sum(p.records for p in group)
        counts = {"new": 0, "ok": 0, "differs": 0}
        for p in group:
            counts[p.status()] += 1
        todo_bytes += sum(p.expected_bytes for p in group if p.status() != "ok")
        have_bytes += sum(p.expected_bytes for p in group if p.status() == "ok")
        state = "to fetch" if counts["new"] else "have it"
        if counts["differs"]:
            state = f"{counts['differs']} stale"
        print(f"{'/'.join(key):<22}{len(group):>7}{recs:>12,}{_mb(size):>11}{state:>22}")

    print("-" * 76)
    print(f"to fetch {_mb(todo_bytes)}  /  already have {_mb(have_bytes)}")
    if any(p.status() == "differs" for p in parts):
        print("\nStale means the local copy is an older export. Use --force to refresh.")
    print("\nNot fetched on purpose:")
    for (cat, ds), why in SKIPPED.items():
        print(f"  - {cat}/{ds:<12} {why}")
    print(f"\nStored under: {DUMP_ROOT}")


def cmd_download(only: set[str] | None, force: bool, workers: int) -> None:
    ensure_dirs()
    parts, meta = load_parts(only)
    todo = [p for p in parts if force or p.status() != "ok"]
    if not todo:
        print("nothing to fetch — everything is current.")
        return

    total = sum(p.expected_bytes for p in todo)
    print(f"{len(todo)} files / about {_mb(total)} — {workers} at a time\n")
    done = {"n": 0, "bytes": 0}
    failed: list[tuple[Part, str]] = []

    def fetch(p: Part) -> None:
        try:
            size = stream_to_file(p.url, p.path)
        except Exception as exc:            # noqa: BLE001 — one bad file must not stop the rest
            failed.append((p, repr(exc)))
            print(f"  x {p.name} {p.path.name}: {exc}", flush=True)
            return
        done["n"] += 1
        done["bytes"] += size
        print(f"  o [{done['n']}/{len(todo)}] {p.name:<18} {p.path.name}  {_mb(size)}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fetch, todo))

    print(f"\ndone {done['n']}/{len(todo)} ({_mb(done['bytes'])})")
    for key, m in meta.items():
        print(f"  {'/'.join(key):<22} export {m['export_date']}  {m['total_records']:,} records")
    if failed:
        print(f"\n{len(failed)} failed — re-run the same command to resume just those:")
        for p, err in failed:
            print(f"  - {p.path.name}: {err}")
        raise SystemExit(1)


def cmd_verify(only: set[str] | None) -> None:
    """Check each zip opens and holds as many records as the manifest promised.

    Kept out of the download path on purpose: a one-off check that moves into the
    hot path is exactly the pattern docs/14 warns about.
    """
    parts, _ = load_parts(only)
    bad = 0
    for p in parts:
        if not p.path.exists():
            print(f"  - {p.path.name}: absent")
            continue
        try:
            with zipfile.ZipFile(p.path) as z:
                inner = [n for n in z.namelist() if n.endswith(".json")]
                with z.open(inner[0]) as fh:
                    n = len(json.load(fh).get("results") or [])
        except Exception as exc:            # noqa: BLE001
            print(f"  x {p.path.name}: will not open — {exc}")
            bad += 1
            continue
        ok = not p.records or n == p.records
        bad += not ok
        print(f"  {'o' if ok else 'x'} {p.path.name:<44} {n:>9,} (manifest {p.records:,})")
    print(f"\n{bad} problem(s)")
    if bad:
        raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["plan", "download", "verify"])
    ap.add_argument("--only", default=None,
                    help="comma-separated dataset names, e.g. ndc,nsde (default: all)")
    ap.add_argument("--force", action="store_true", help="re-fetch even if present")
    ap.add_argument("--workers", type=int, default=4, help="concurrent downloads (default 4)")
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
