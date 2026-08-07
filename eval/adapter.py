"""Swap only the pipeline's document-delivery path for local fixtures.

The real graph runs in agent mode exactly as it ships; the three seams where it
reaches outside are replaced. Everything that decides anything — the candidate
selector, parallel traversal, the critics, synthesis — is untouched production code.

  seam 1  resolve_digital_link tool -> reads linksets and pages from the fixture tree
  seam 2  resolver.fetch_bytes      -> reads indexing bytes from fixture files
  seam 3  image-question tool       -> rewrites the URL to a base64 data URL first

No core file is edited. Every dependence on core internals (_tools,
_image_question_tool) is confined to this module.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

BENCH_ROOT = Path(__file__).resolve().parents[1]          # benchmark/
REPO_ROOT = BENCH_ROOT.parent                              # gs1-palantir/
CORE_ROOT = REPO_ROOT / "gs1-palantir-core-kg_neo4j"

_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
}


def setup_core_env() -> None:
    """Call before importing the core: loads .env and extends sys.path.

    The core's load_dotenv() resolves against the working directory, so running
    from benchmark/ would never find kg_neo4j/.env. Load it explicitly instead.
    """
    from dotenv import load_dotenv

    for env in (BENCH_ROOT / ".env", CORE_ROOT / ".env"):
        if env.exists():
            load_dotenv(env, override=False)
    if str(CORE_ROOT) not in sys.path:
        sys.path.insert(0, str(CORE_ROOT))


class FixtureResolver:
    """Maps a GS1 Digital Link path or URL onto a file in the fixture tree.

    "/01/00000050457250?linktype=linkset"            → <root>/01-00000050457250/linkset.json
    "/01/00000050457250/pages/pip.html"              → <root>/01-00000050457250/pages/pip.html
    "https://id.oliot.org/414/9520000000073/media/img-1.jpg"
                                                     → <root>/414-9520000000073/media/img-1.jpg
    """

    def __init__(self, corpus_root: Path) -> None:
        self.corpus_root = Path(corpus_root).resolve()
        if not self.corpus_root.is_dir():
            raise FileNotFoundError(f"corpus root not found: {self.corpus_root}")

    # -- path mapping ------------------------------------------------------

    def to_file(self, path_or_url: str) -> Path:
        parts = urlsplit(str(path_or_url))
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) < 2:
            raise FileNotFoundError(f"not a Digital Link path: {path_or_url!r}")
        folder = self.corpus_root / f"{segments[0]}-{segments[1]}"
        if not folder.is_dir():
            raise FileNotFoundError(f"unknown entity {segments[0]}/{segments[1]} (no {folder.name}/)")
        rest = segments[2:]
        wants_linkset = any(
            k.lower() == "linktype" and v.lower() == "linkset" for k, v in parse_qsl(parts.query)
        )
        if wants_linkset or not rest:
            target = folder / "linkset.json"
        else:
            target = folder.joinpath(*rest)
        target = target.resolve()
        if self.corpus_root not in target.parents:
            raise FileNotFoundError(f"path escapes corpus root: {path_or_url!r}")
        if not target.is_file():
            raise FileNotFoundError(f"fixture file not found: {target}")
        return target

    # -- seam 1: resolve_digital_link -------------------------------------

    def resolve(self, path: str) -> Any:
        """The local tools: a linkset comes back as a dict, a page as text."""
        target = self.to_file(path)
        if target.name == "linkset.json":
            return self._serve_linkset(target)
        if target.suffix.lower() in _MIME_BY_SUFFIX:
            return self.to_data_url(target)          # a direct media request becomes a data URL
        return self._decode_page(target.read_bytes())

    def _serve_linkset(self, target: Path) -> dict[str, Any]:
        """Serve linkset.json with relative hrefs resolved against the anchor.

        The dataset file is never rewritten; this happens at response time. The URLs
        must be absolute because the candidate selector copies them into source_url,
        and traversal and indexing need that entity context to find the folder again.
        """
        payload = json.loads(target.read_text(encoding="utf-8"))
        for ctx in payload.get("linkset", []):
            anchor = str(ctx.get("anchor", "")).rstrip("/")
            for key, value in ctx.items():
                if key == "anchor" or not isinstance(value, list):
                    continue
                for entry in value:
                    href = entry.get("href") if isinstance(entry, dict) else None
                    if href and "://" not in href and not href.startswith("data:"):
                        entry["href"] = f"{anchor}/{href}"
        return payload

    @staticmethod
    def _decode_page(raw: bytes) -> str:
        """EUC-KR fallback decode, matching validate.read_page."""
        head = raw[:200].decode("ascii", "ignore")
        return raw.decode("cp949" if "euc-kr" in head.lower() else "utf-8")

    # -- seam 2: bytes handed to the indexer -------------------------------

    def fetch_bytes_local(self, href: str) -> bytes:
        return self.to_file(href).read_bytes()

    # -- seam 3: image URL -> data URL -------------------------------------

    def to_data_url(self, path_or_url: str | Path) -> str:
        target = Path(path_or_url) if isinstance(path_or_url, Path) else self.to_file(path_or_url)
        mime = _MIME_BY_SUFFIX.get(target.suffix.lower(), "application/octet-stream")
        return f"data:{mime};base64,{base64.b64encode(target.read_bytes()).decode('ascii')}"


def build_pipeline(corpus_root: Path):
    """The real ChatPipeline with the three seams replaced."""
    setup_core_env()

    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    from packages.agents.nodes.digital_link_explorer import (
        IMAGE_TOOL_NAME,
        ImageQuestionInput,
        build_image_question_tool,
    )
    from packages.agents.state_graph import ChatPipeline
    from packages.vector_index import resolver as vres

    fixtures = FixtureResolver(corpus_root)
    pipeline = ChatPipeline()
    explorer = pipeline.external_retriever.digital_link_node

    # seam 1 — the linkset and page lookup tools
    class _ResolveInput(BaseModel):
        path: str = Field(..., description="GS1 Digital Link path, e.g. /01/00000050457250/pages/pip.html")

    async def resolve_digital_link(path: str) -> Any:
        return fixtures.resolve(path)

    explorer._tools = {
        "resolve_digital_link": StructuredTool.from_function(
            coroutine=resolve_digital_link,
            name="resolve_digital_link",
            description=(
                "Resolve a GS1 Digital Link path. Returns the linkset for "
                "?linktype=linkset, else the target resource content."
            ),
            args_schema=_ResolveInput,
        )
    }

    # seam 3 — image questions: rewrite the URL, then call the original tool
    inner_image_tool = build_image_question_tool(pipeline.llm)

    async def answer_from_image(user_query: str, image: str) -> Any:
        if not image.startswith("data:"):
            image = fixtures.to_data_url(image)
        return await inner_image_tool.ainvoke({"user_query": user_query, "image": image})

    explorer._image_question_tool = StructuredTool.from_function(
        coroutine=answer_from_image,
        name=IMAGE_TOOL_NAME,
        description=inner_image_tool.description,
        args_schema=ImageQuestionInput,
    )

    # seam 2 — swap the module attribute so no outbound fetch can happen
    vres.fetch_bytes = fixtures.fetch_bytes_local

    return pipeline, fixtures
