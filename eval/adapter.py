"""Doc 04 §1.2 — 방법 2 어댑터: 파이프라인의 "문서 배달 경로"만 로컬 fixture로 바꾼다.

실그래프(agent 모드)를 그대로 돌리되, 외부 접근이 일어나는 seam 세 곳만 교체한다.
판단하는 로직(후보 선택 LLM, 병렬 traversal, critic, 합성)은 전부 실코드 그대로.

  seam 1: resolve_digital_link 도구  → linkset·페이지를 fixture 폴더에서 읽는 로컬 도구
          (explorer._tools 캐시에 미리 꽂는다 — digital_link_explorer.py:464-469)
  seam 2: resolver.fetch_bytes       → 인덱싱 바이트를 fixture 파일에서 읽는다
          (vector_index/resolver.py:75 를 런타임 교체)
  seam 3: 이미지 질문 도구            → URL을 data URL(base64)로 바꾼 뒤 원본 도구 호출
          (explorer._image_question_tool 캐시에 미리 꽂는다)

코어 코드는 한 줄도 수정하지 않는다. 코어 내부 속성(_tools, _image_question_tool)에
의존하는 지점은 전부 이 파일 안에만 있다 (doc 04 §9 리스크 참조).
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
    """코어 import 전에 호출: .env 로드 + sys.path (validate.py와 같은 패턴).

    코어 config.py의 load_dotenv()는 cwd 기준이라 benchmark/에서 실행하면
    kg_neo4j/.env를 못 찾는다 — 여기서 명시적으로 로드해 둔다.
    """
    from dotenv import load_dotenv

    for env in (BENCH_ROOT / ".env", CORE_ROOT / ".env"):
        if env.exists():
            load_dotenv(env, override=False)
    if str(CORE_ROOT) not in sys.path:
        sys.path.insert(0, str(CORE_ROOT))


class FixtureResolver:
    """GS1 Digital Link 경로/URL → fixture 파일 매핑.

    "/01/00000050457250?linktype=linkset"            → <root>/01-00000050457250/linkset.json
    "/01/00000050457250/pages/pip.html"              → <root>/01-00000050457250/pages/pip.html
    "https://id.oliot.org/414/9520000000073/media/img-1.jpg"
                                                     → <root>/414-9520000000073/media/img-1.jpg
    """

    def __init__(self, corpus_root: Path) -> None:
        self.corpus_root = Path(corpus_root).resolve()
        if not self.corpus_root.is_dir():
            raise FileNotFoundError(f"corpus root not found: {self.corpus_root}")

    # -- 경로 매핑 ---------------------------------------------------------

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
        """로컬 도구의 본체. linkset은 dict, 페이지는 텍스트를 돌려준다."""
        target = self.to_file(path)
        if target.name == "linkset.json":
            return self._serve_linkset(target)
        if target.suffix.lower() in _MIME_BY_SUFFIX:
            return self.to_data_url(target)          # 미디어를 직접 물으면 data URL로
        return self._decode_page(target.read_bytes())

    def _serve_linkset(self, target: Path) -> dict[str, Any]:
        """linkset.json을 읽고 상대 href를 anchor 기준 절대 URL로 바꿔서 서빙.

        데이터셋 파일은 건드리지 않는다 — 응답 시점 변환(doc 04 §1.2 원칙 2).
        절대화하는 이유: 후보 선택 LLM이 복사하는 source_url에 엔티티 맥락이
        담겨야 traversal·인덱싱이 어느 폴더인지 되찾을 수 있다.
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
        """EUC-KR 폴백 디코드 (validate.py read_page와 같은 규칙)."""
        head = raw[:200].decode("ascii", "ignore")
        return raw.decode("cp949" if "euc-kr" in head.lower() else "utf-8")

    # -- seam 2: 인덱싱 바이트 ---------------------------------------------

    def fetch_bytes_local(self, href: str) -> bytes:
        return self.to_file(href).read_bytes()

    # -- seam 3: 이미지 → data URL -----------------------------------------

    def to_data_url(self, path_or_url: str | Path) -> str:
        target = Path(path_or_url) if isinstance(path_or_url, Path) else self.to_file(path_or_url)
        mime = _MIME_BY_SUFFIX.get(target.suffix.lower(), "application/octet-stream")
        return f"data:{mime};base64,{base64.b64encode(target.read_bytes()).decode('ascii')}"


def build_pipeline(corpus_root: Path):
    """seam 3곳을 교체한 실파이프라인(ChatPipeline)을 돌려준다."""
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

    # seam 1 — linkset·페이지 조회 도구
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

    # seam 3 — 이미지 질문 도구 (URL → data URL 변환 후 원본 도구 호출)
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

    # seam 2 — 인덱싱 바이트 (module attribute 교체; 외부 fetch는 이제 일어날 수 없다)
    vres.fetch_bytes = fixtures.fetch_bytes_local

    return pipeline, fixtures
