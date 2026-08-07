"""Paths and env loading for the benchmark toolchain.

Env files are searched in order: benchmark/.env, then the pipeline's
kg_neo4j/.env (where TOUR_API_KEY currently lives). Values already set in
the process environment win over both.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BENCH_ROOT = Path(__file__).resolve().parents[2]          # benchmark/
REPO_ROOT = BENCH_ROOT.parent                              # gs1-palantir/

DATA_DIR = BENCH_ROOT / "data"
RAW_OFF_DIR = DATA_DIR / "raw" / "off"
RAW_TOUR_DIR = DATA_DIR / "raw" / "tour"
RAW_PHARMA_DIR = DATA_DIR / "raw" / "openfda"
WORK_DIR = BENCH_ROOT / "work"
SCHEMA_DIR = BENCH_ROOT / "schemas"
ENTITIES_DIR = BENCH_ROOT / "entities"

_ENV_CANDIDATES = [
    BENCH_ROOT / ".env",
    REPO_ROOT / "gs1-palantir-core-kg_neo4j" / ".env",
]
for _env in _ENV_CANDIDATES:
    if _env.exists():
        load_dotenv(_env, override=False)

TOUR_API_KEY = os.getenv("TOUR_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

USER_AGENT = "gs1-palantir-benchmark/0.1 (sunghakbusiness@gmail.com)"


def ensure_dirs() -> None:
    for d in (RAW_OFF_DIR, RAW_TOUR_DIR, RAW_PHARMA_DIR, WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)
