# 02 — 구현 계획

> 작성: 2026-07-08. 설계 근거: `01-benchmark-dataset-pipeline-design.md` (이하 "설계서").
> 이 문서는 설계서의 S1~S9를 **실행 가능한 마일스톤**으로 쪼갠 것이다. 각 마일스톤에
> 산출물과 완료 기준을 붙여, 어느 세션에서 재개해도 다음 할 일이 명확하도록 한다.

---

## 0. 폴더 구조 (구현 완료 시점 기준)

```
benchmark/
├── docs/
│   ├── 01-benchmark-dataset-pipeline-design.md
│   ├── 02-implementation-plan.md          # 이 문서
│   ├── 03-pipeline-walkthrough.md         # 예시 엔티티 워크스루
│   ├── 04-evaluation-system-design.md     # 평가 시스템 설계
│   ├── 05-experiment-budget.md            # 실험 규모·비용·시간 계획
│   └── 06-pipeline-findings.md            # 평가 중 발견한 파이프라인 이슈 (팀 보고용)
├── schemas/                               # jsonschema — 산출물 형식의 단일 정의
│   ├── fact.schema.json
│   ├── qa.schema.json
│   └── linkset.schema.json
├── scripts/
│   ├── common/
│   │   ├── config.py                      # env 로딩 (TOUR_API_KEY 등)
│   │   ├── identifiers.py                 # GTIN/GLN 체크디지트 (mod-10), 952 GLN 발급
│   │   └── fetch.py                       # UA/Accept 헤더 공통 HTTP (OFF·이미지 프록시 대응)
│   ├── clients/
│   │   ├── off_client.py                  # 덤프 스트리밍 필터 + 개별 제품 조회
│   │   └── tour_client.py                 # KorService2 4개 엔드포인트 래퍼
│   ├── select_entities.py                 # S1 → candidates 리포트
│   ├── extract_facts.py                   # S3 → facts.jsonl
│   ├── build_fixtures.py                  # S4~S7 (+ --overrides로 S8 겸용)
│   ├── gen_qa.py                          # S9 초안 생성 + 검수 시트 export
│   ├── validate.py                        # 설계서 §4 검사 일괄 실행
│   └── templates/                         # T1~T4 (Jinja2)
│       ├── t1_plain.html.j2
│       ├── t2_table.html.j2
│       ├── t3_jsonld.html.j2
│       └── t4_noisy.html.j2
├── data/                                  # (gitignore) 원천 데이터
│   ├── raw/off/                           # OFF 덤프
│   └── raw/tour/                          # TourAPI 응답 캐시
├── work/                                  # (gitignore) 중간 산출물
│   ├── candidates-food.jsonl              # S1 출력
│   ├── candidates-place.jsonl
│   ├── selection.yaml                     # ★사람 입력: 10+10 선정
│   ├── cf-plan.yaml                       # ★사람 입력: 변조 대상 팩트 선정
│   └── qa-review.csv                      # ★사람 입력: QA 검수 시트
├── manifest.json                          # ↓ 여기부터 최종 산출물 (설계서 §3)
├── facts.jsonl
├── qa.jsonl
├── entities/…
└── counterfactual/…
```

원칙:
- **최종 산출물(manifest, facts, qa, entities, counterfactual)만 저장소에 커밋**, data/·work/는 gitignore.
  단 `selection.yaml`·`cf-plan.yaml`은 사람 결정의 기록이므로 커밋 대상.
- 파이프라인 코드(packages/…)에 **의존하지 않는다** — 벤치마크는 독립 툴체인 (설계 원칙 7의 코드판).
  유일한 예외: validate.py의 EUC-KR 복원 검사에서 파이프라인 `_decode`를 optional import (없으면 skip).

## 1. 기술 결정

| 항목 | 결정 | 이유 |
|---|---|---|
| 언어/버전 | Python 3.12 (로컬 miniforge 기준) | 파이프라인과 동일 환경 |
| 의존성 | `httpx`(또는 requests), `jinja2`, `jsonschema`, `python-dotenv`, `pyyaml` | 전부 경량·표준적. pandas 불필요 (덤프는 스트리밍) |
| LLM 호출 | OpenAI API (게이트 LLM 판정, QA 초안, 이미지 팩트 추출) | `.env`의 기존 키 재사용 |
| 템플릿 | Jinja2 | 팩트→페이지 렌더링의 표준 도구 |
| env 로딩 | `config.py`가 `benchmark/.env` → `gs1-palantir-core-kg_neo4j/.env` 순으로 탐색 | TOUR_API_KEY가 현재 후자에 있음 (2026-07-08 저장) |
| OFF 덤프 | `openfoodfacts-products.jsonl.gz`를 gzip 스트리밍(라인 단위)으로 1패스 필터 | 압축 ~10GB, 해제 50GB+ — 메모리에 올리지 않음 |
| 스키마 검증 | 모든 jsonl 쓰기 직후 jsonschema 검증 (쓰는 쪽에서 실패) | validate.py는 최종 회귀 확인용, 1차 방어는 생성기 |

## 2. 마일스톤

### M0 — 스캐폴딩 + 공통 유틸 (0.5일)

- 폴더 구조 생성, `.gitignore`(data/, work/의 비결정 파일), `requirements.txt`.
- `identifiers.py`: GTIN-13→14 변환, mod-10 체크디지트 계산·검증, `952` 프리픽스 GLN 발급기(일련번호 인자).
- `fetch.py`: 기본 헤더 `User-Agent: gs1-palantir-benchmark/0.1 (연락 이메일)` + 이미지 요청 시
  `Accept: image/avif,image/webp,image/*` (실측: OFF는 UA 없으면 차단, TourAPI 이미지 프록시류는 Accept 없으면 400).
- `schemas/` 3종 초안 (설계서 S3·S9·S7의 JSON 예시를 형식화).
- **완료 기준**: `pytest` — 체크디지트 왕복 테스트(실제 GTIN 5개 + 952 GLN 생성분) 통과.

### M1 — 소스 클라이언트 (0.5일)

- `off_client.py`:
  - `stream_dump(path, fields) -> Iterator[dict]` — gzip 라인 스트리밍, 필요 필드만 투영.
  - `get_product(gtin)` — 개별 조회 (fetch.py 경유). 응답을 `data/raw/off/`에 캐시.
- `tour_client.py`: `search(keyword)`, `common(cid)`, `intro(cid, type_id)`, `images(cid)` —
  검증된 KorService2 4개 엔드포인트(설계서 §1.2), 응답 원문을 `data/raw/tour/`에 캐시
  (일 1,000건 제한 방어 + 재현성).
- **완료 기준**: 광안대교(128164)와 실존 GTIN 1개로 라이브 왕복 성공, 캐시 재실행 시 API 미호출.

### M2 — S1 역선택 (0.5일 + ★사람 1~2시간)

- `select_entities.py food`: OFF 덤프 1패스 → 설계서 S1 점수식 → 상위 30을
  `work/candidates-food.jsonl`로 (점수 내역·이미지 URL·카테고리 포함).
- `select_entities.py place`: 부산 지역 목록(areaBasedList2) + 전국 유명 명소 시드 → TourAPI 스코어링 → `candidates-place.jsonl`.
- 후보 리포트를 사람이 보기 좋은 markdown 표로도 출력 (`work/candidates-report.md`).
- **★사람 작업**: 리포트 보고 `selection.yaml` 작성 — 음식 10(카테고리 5종+, 알레르겐 보유 5+,
  동일 브랜드 2~3), 장소 10(부산 다수 + 타 지역).
- **완료 기준**: selection.yaml의 20개 전부가 후보 파일에 존재하고 다양성 제약 충족 (스크립트가 검사).

### M3 — S3 팩트 추출 (0.5~1일)

- `extract_facts.py`: selection.yaml → 소스 캐시에서 팩트 추출 → `facts.jsonl`.
  - 규칙 기반이 기본 (nutriments·usetime 등 필드 매핑). 텍스트 정규화: TourAPI 비정형 값
    ("상시 개방" 등)은 원문 보존 + 정규화 필드 병기.
  - **패시지+span**: 팩트가 소스 산문(overview 등)에 실재하면 해당 문단을 `source.passage`로,
    값 위치를 `value_span`으로 저장 (설계서 S3 — 페이지 매몰 렌더링과 CF span 치환의 재료).
  - 라벨 사진 교차확인(`--verify-images`): VLM 판독과 텍스트 필드의 불일치를
    `work/fact-conflicts.md` + `work/image-qa-exclusions.json`으로 기록. **텍스트가 정본**
    (값 교정 없음 — 설계서 §1.1 정책), 제외 목록은 M6 gen_qa가 이미지 QA에서 사용.
  - 관계 팩트(brand, located_in_region) 포함 — multi-hop 재료.
- **완료 기준**: 엔티티 20개 전부 팩트 생성, 스키마 통과, 제외 목록 생성됨.

### M4 — S4~S7 fixture 빌드 (1~1.5일)

- `build_fixtures.py`:
  1. 게이트 실행 (규칙 + LLM 판정, 설계서 S4) → 통과/탈락 리포트를 manifest에.
     탈락 발생 시 여기서 멈추고 M2 충원으로 되돌아감.
  2. 템플릿 T1~T4 순환 배정 (엔티티 ID 해시 기반 — 결정적) → linktype당 1페이지 렌더링.
     T4의 EUC-KR 변형은 장소 페이지 일부에만. 본문 조립은 설계서 S5의 난이도 노브를 따름:
     패시지 매몰 + distractor 문단 + 표 내 100g/1회제공량 병기. (LLM 증량은 TODO — 설계서 S5 참조.)
  3. 미디어 다운로드 (엔티티당 2~4장 캡, cpyrhtDivCd Type1만) → `entities/*/media/`.
  4. linkset.json 조립 (RFC 9264, MIME 정확성 — 이미지 항목 정식 등록).
  5. manifest.json 갱신 (출처·라이선스·팩트 배치표·생성 파라미터·템플릿 배정표).
- `--overrides work/…` 옵션: facts에 오버라이드 적용 후 출력 루트만 `counterfactual/`로 바꿔 동일 실행 (S8 겸용).
- **완료 기준**: 20 엔티티 × (linkset + 페이지 + 미디어) 생성, validate.py 검사 1~4 통과.

### M5 — S8 counterfactual + validate (0.5~1일)

- **★사람 작업**: `work/counterfactual-overrides.jsonl` — 변조 대상 20~30 팩트 선정 (prior 강할 것 우선) + 변조값.
  (2026-07-08: Claude 초안 29건을 사용자 검토 방식으로 진행 — 검토표 temp.md)
- `build_fixtures.py --overrides` 실행 → `counterfactual/` 생성.
- `validate.py` 완성: 설계서 §4의 7개 검사 (스키마 / 체크디지트 / 링크 무결성 / 게이트 리포트 /
  팩트 유일 배치·렌더링 확인 / CF 발산 / EUC-KR 복원).
- **완료 기준**: `validate.py` 전 항목 green, CF 발산 검사에서 변조 팩트 전건이 원본과 다른 값으로 확인.

### M6 — S9 QA 생성 + 검수 (1일 + ★검수 반나절)

- `gen_qa.py`:
  - linktype 페이지별 LLM 초안 2~3개 (gold_fact_ids 인용 강제, modality·hop 태그 자동 부여).
  - multi-hop은 관계 팩트가 실재하는 조합만 생성 (동일 브랜드·동일 행정구역 — 스크립트가 관계 그래프에서 후보 조합 열거).
  - modality 배분 목표(html 70 / image 25 / video 5) 미달 시 경고.
  - 골드 제외 목록 2종 준수: `work/image-qa-exclusions.json`(사진≠텍스트 쌍 — 이미지 QA 금지),
    `work/qa-ambiguous-facts.json`(소스 데이터가 토큰을 두 페이지에 중복 보유 — 골드 사용 금지, validate가 생성).
  - `work/qa-review.csv` export (질문/골드/근거 페이지 링크/판정 열).
- **★사람 작업**: 전수 검수 (음식은 라벨 사진 대조) → 판정 반영해 `qa.jsonl` 확정.
- **완료 기준**: 160~240쌍, validate 검사 5(gold_fact_ids 무결성) 통과, 검수 완료율 100%.

### 이후 (이 계획 범위 밖)

- doc 04: 평가 하네스 설계 (블랙박스 + trace 파서 2층, 설계서 §5) → 별도 계획으로.
- 설계서 §9 phase 2 (KG 에피소드, stale, 합성 엔티티).

## 3. 사람 개입 지점 요약 (전부 파일 인터페이스)

| 시점 | 입력 파일 | 내용 |
|---|---|---|
| M2 | `work/selection.yaml` | 후보 30+α에서 10+10 선정 |
| M3 | `work/fact-conflicts.md` 판정 | 라벨 사진 vs 텍스트 불일치 중재 |
| M5 | `work/cf-plan.yaml` | 변조 팩트 선정·변조값 |
| M6 | `work/qa-review.csv` | QA 전수 검수 |

## 4. 선행 결정 필요 (블로커)

| 결정 | 필요 시점 | 기본값 (미결정 시) |
|---|---|---|
| Wikidata masterData 포함 여부 | M3 시작 전 | 제외 (소스 필드 조합으로 masterData 생성) |
| QA 언어 정책 (cross-lingual 여부) | M6 시작 전 | 문서 언어 = 질문 언어 + cross-lingual 10~20% 태그 부착 |
| OFF 덤프 다운로드 (~10GB) 시점·위치 | M1 전 | `data/raw/off/`, 야간 다운로드 |

## 5. 일정 합계

| | M0 | M1 | M2 | M3 | M4 | M5 | M6 | 계 |
|---|---|---|---|---|---|---|---|---|
| 구현 | 0.5 | 0.5 | 0.5 | 0.5~1 | 1~1.5 | 0.5~1 | 1 | **4.5~6일** |
| ★사람 | — | — | 1~2h | 판정 수시 | — | 30m | 반나절 | |

설계서 §6 견적(5~6일)과 정합. M0~M1은 세션 1개, M2~M3 세션 1개, M4 세션 1~2개, M5~M6 세션 1~2개가
현실적 단위다. 각 마일스톤 완료 시 log.md에 상태 기록.
