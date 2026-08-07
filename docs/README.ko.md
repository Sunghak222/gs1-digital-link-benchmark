# GS1 Digital Link 멀티모달 RAG 벤치마크 (한국어판)

> 영어 원본: [../README.md](../README.md)
> 이 문서는 **v1.0 시점 기준**입니다. 이후 확장 트랙에서 linktype 명칭이 GS1 공식명으로
> 개정되고(nutritionalInfo 등) ingredientsInfo·hasRetailers 페이지가 추가되었습니다 —
> 최신 구조는 영어 README와 `docs/10`을 보세요.

GS1 Digital Link 리졸버를 따라가며 답을 찾는 RAG 파이프라인을 평가하기 위한 벤치마크입니다.
공개 데이터(Open Food Facts, 한국관광공사 TourAPI)에서 출발해 **링크셋 + HTML 페이지 + 이미지로 이루어진 픽스처 코퍼스**와 **221개의 QA**를 반자동으로 구축했습니다.

왜 직접 만들었나 하면 — GS1 Digital Link는 바코드 하나로 제품의 영양정보·인증·이미지 같은 리소스에 연결해 주는 표준인데, 이 링크셋을 실제로 운영하는 서비스가 아직 드뭅니다. 평가할 대상은 있는데 평가할 데이터가 없는 상황이라, 벤치마크를 데이터부터 만들었습니다.

## 핵심 설계

**팩트가 단일 원천입니다.** 소스 API 응답을 원자 팩트(`facts.jsonl`, 407건)로 쪼개고, HTML 페이지·QA 정답·변조 코퍼스를 전부 여기서 파생시킵니다. 페이지에 실린 값과 채점 기준이 같은 곳에서 나오므로, 정답지가 틀릴 여지가 구조적으로 없습니다.

**Counterfactual 코퍼스가 사전지식 컨닝을 잡아냅니다.** 엔티티가 유명한 것들이라(경복궁, Heinz 케첩...) LLM이 문서를 안 읽고도 답을 맞힐 수 있습니다. 그래서 팩트 102개를 일부러 다른 값으로 바꾼 평행 코퍼스를 하나 더 만들었습니다. 같은 질문을 두 코퍼스에서 던져서 답이 갈리면 문서를 읽은 것이고, 둘 다 원래 값이면 사전지식으로 답한 것입니다.

**원문 문단을 통째로 싣습니다.** 추출한 핵심만 페이지에 넣으면 문제가 너무 쉬워집니다. 그래서 팩트가 파묻혀 있던 원문 문단(passage)을 그대로 싣고, 값의 위치(value_span)만 별도로 기록해 둡니다. 이 위치는 counterfactual을 만들 때 그 자리만 도려내는 데도 쓰입니다.

**언제 다시 돌려도 같은 결과가 나옵니다.** API 응답은 스냅샷으로 캐시되고, 추출은 결정적 규칙이며, LLM을 쓰는 세 곳(소개글 사실 추출, 라벨 사진 대조, QA 초안)도 전부 temperature 0 + 디스크 캐시입니다.

## 데이터셋 규모

| 항목 | 내용 |
|---|---|
| 엔티티 | 20개 — 음식 10(글로벌/영어, 실제 GTIN) + 관광지 10(한국어, 데모 GLN) |
| 팩트 | 407건 (`facts.jsonl`) |
| 페이지 | 말뭉치당 130장 — 템플릿 4종 순환(평문/표/JSON-LD/노이즈), EUC-KR 인코딩 케이스 포함 |
| 이미지 | 58장 — 음식 30장(정면/영양라벨/성분표) + 장소 28장 |
| QA | 221건 — html 210 / image 11, 영어 126 / 한국어 95, 멀티홉 5, cross-lingual 포함 |
| 코퍼스 | 2벌 — 원본(`entities/`) + counterfactual(`counterfactual/`, 102개 팩트 변조) |

## 저장소 구조

이 저장소는 **시험장**이라고 생각하면 됩니다. 응시자(평가받는 RAG 파이프라인)가 보는 시험지가 있고, 채점자만 들고 있는 정답지가 있습니다. 여기 있는 모든 파일은 둘 중 한쪽에 속합니다.

**응시자가 보는 것** — 엔티티 하나당 폴더 하나이고, 실제 GS1 리졸버가 서빙하는 모양 그대로입니다:

```
entities/
└── 01-00000050457250/               # 엔티티 1개 = 폴더 1개 (이건 Heinz 케첩;
    │                                #   "01/"은 상품 GTIN, "414/"는 장소 GLN)
    ├── linkset.json                 # 입구 — "이 상품엔 영양 페이지, 알러지 페이지,
    │                                #   사진 3장이 있다"는 목차 + 각각의 링크
    ├── pages/
    │   ├── pip.html                 # 상품 소개 (이름·브랜드·카테고리)
    │   ├── nutritionalInformation.html   # 영양성분표
    │   ├── allergenInformation.html      # 알러지 + 성분 목록
    │   └── ...                      # 엔티티당 5~7장
    └── media/
        ├── front.jpg                # 제품 정면 사진
        ├── nutrition-label.jpg      # 영양라벨 사진 (이미지 QA가 읽는 대상)
        └── ingredients.jpg
```

`counterfactual/`은 똑같은 20개 폴더인데, 페이지 속 값 102개를 일부러 다르게 바꿔 놓았습니다. 문서를 진짜 읽는 모델은 여기서 다른 답을 내고, 외운 지식으로 답하는 모델은 같은 답을 냅니다.

**채점자가 들고 있는 것** — 정답지. 파이프라인에게는 절대 안 보여줍니다:

| 파일 | 정체 | 사람용 버전 |
|---|---|---|
| `facts.jsonl` | 407개 팩트 전부, 한 줄에 하나 — 모든 페이지와 정답의 원본 대장 | `facts.pretty.json` (같은 내용을 제품별 → 페이지별로 묶어 펼친 열람용) |
| `qa.jsonl` | 문제 221개 + 정답 + 근거 팩트 | `qa.csv` (엑셀로 열림) |
| `manifest.json` | 빌드 기록 — 핵심은 *배치 맵*: 각 팩트가 어느 페이지에 인쇄됐는지. 채점자가 "검색기가 어느 페이지를 찾았어야 하나"를 아는 근거 | — |

**만드는 기계** — 데이터셋을 다시 생성하거나 확장할 때만 필요합니다:

| 폴더 | 내용 |
|---|---|
| `scripts/` | 구축 파이프라인: 엔티티 선정 → API 스냅샷 → 팩트 추출 → 페이지·링크셋 렌더링 → 검증 → QA 생성 |
| `work/` | 사람의 결정, 스크립트끼리 주고받는 목록, 검수 기록 — 파일별 역할은 아래 "work/ 내부" 표 |
| `schemas/` | 모든 fact / QA / linkset이 통과해야 하는 JSON Schema |
| `data/raw/` | API 응답·LLM 호출 캐시 (git 제외) — 재실행이 재현되는 이유 |
| `docs/` | 01 설계 근거 / 02 구현 계획 / 03 엔티티 하나로 처음부터 끝까지 따라가는 워크스루 |

### work/ 내부

모든 파일은 사람의 결정이거나, 한 스크립트가 다른 스크립트에게 만들어 주는 목록이거나, 단계 기록입니다.
앞의 다섯은 **live** — 경로가 `scripts/`에 박혀 있어 이름을 바꾸려면 스크립트도 함께 고쳐야 합니다.

| 파일 | 역할 | 만든 곳 → 읽는 곳 |
|---|---|---|
| `selection.yaml` | ★ 사람 결정 — 후보 중 최종 선정한 엔티티 20개 | 사람 → extract_facts |
| `counterfactual-overrides.jsonl` | ★ 사람 결정 — 어떤 팩트를 변조할지; counterfactual 코퍼스의 유일한 입력 | 사람 → build_fixtures, validate, gen_qa |
| `entity-map.json` | 엔티티 ID ↔ 클래스·이름·source id | extract_facts → build_fixtures, gen_qa, validate |
| `image-qa-exclusions.json` | 라벨 사진과 텍스트가 어긋나는 (엔티티, 영양소) 쌍 — 이미지 QA 금지 목록 | extract_facts → gen_qa |
| `qa-ambiguous-facts.json` | 같은 답 토큰이 두 페이지에 있는 팩트 — QA 골드 금지 목록 | validate → gen_qa |
| `fact-conflicts.md` | 라벨 사진 vs 텍스트 불일치의 사람용 리포트 (기계용은 `image-qa-exclusions.json`) | extract_facts → 사람 |
| `candidates-food.jsonl` / `candidates-place.jsonl` | 1단계: 기계 채점한 후보 목록 (`candidates-report.md`는 사람용 표) | select_entities → extract_facts |
| `qa-draft.jsonl` / `qa-review.csv` | 9단계: QA 초안과 검수 시트 — 검수 후 `qa.jsonl`로 확정 | gen_qa ↔ 사람 |
| `notes/` | QA 전수 검수 때의 판정 근거 기록 | 사람 |
| `ppt/` | 팀 발표 계획·파이프라인 다이어그램 | 사람 |

## 파이프라인 — 예시 하나로 따라가기

Heinz 케첩(GTIN 50457250) 하나가 각 단계를 지나는 모습입니다. 전체 이야기는 [docs/03-pipeline-walkthrough.md](docs/03-pipeline-walkthrough.md)에 있습니다.

**① 후보를 모으고 사람이 고릅니다** (`select_entities.py`). OFF 검색에서 자료가 풍부한 제품을 기계 채점하고, 후보표를 보고 카테고리·알러젠이 겹치지 않게 10개를 `work/selection.yaml`에 확정합니다.

```yaml
- "50457250"   # Heinz Tomato Ketchup — 소스류 / celery / GTIN-8 케이스
```

**② API 응답을 스냅샷으로 저장합니다** (`clients/`). 이후 모든 단계는 이 캐시만 읽으므로, OFF가 내일 값을 바꿔도 벤치마크는 그대로입니다.

**③ 팩트로 쪼갭니다** (`extract_facts.py`). regex 파싱이 아니라 "이 필드는 이 술어, 이 페이지"라는 매핑 규칙의 나열입니다. Heinz에서 25개가 나옵니다.

```json
{"fact_id": "food-50457250-sugars_100g", "value": {"amount": 22.8, "unit": "g"},
 "linktype": "nutritionalInformation", "source": {"origin": "off", "field": "nutriments.sugars_100g"}}
```

장소의 소개글처럼 필드가 없는 산문에서만 LLM을 씁니다 — 사실이 적힌 부분을 글에서 그대로 복사해 오게 하고, 코드가 원문 검색으로 재확인해서 못 찾으면 버립니다. LLM이 값을 지어낼 수 없는 구조입니다.

**④ 페이지·이미지·링크셋을 조립합니다** (`build_fixtures.py`). 팩트만 읽습니다. 같은 linktype 팩트끼리 Jinja2 템플릿에 부어 HTML을 만들고, 존재하는 페이지들을 RFC 9264 링크셋으로 묶습니다.

```html
<tr><td>sugars</td><td>22.8 g</td><td>3.42 g</td></tr>
```

**⑤ 같은 빌더를 변조표와 함께 한 번 더 돌립니다** (`--overrides`). Heinz 당류를 22.8→13.1g으로(1회 제공량도 비례해서) 바꾼 counterfactual 코퍼스가 나옵니다.

```
원본: <td>sugars</td><td>22.8 g</td><td>3.42 g</td>
CF  : <td>sugars</td><td>13.1 g</td><td>1.97 g</td>
```

**⑥ 검증하고 QA를 만듭니다** (`validate.py`, `gen_qa.py`). 7종 무결성 검사(팩트가 페이지에 실렸는지, 정답이 한 페이지에만 있는지, CF가 전부 발산했는지 등)를 통과시킨 뒤, 페이지별로 LLM이 질문을 만들되 **정답은 그 페이지의 팩트 중에서만 고르게 강제**합니다. 초안 230건을 전수 검수해서(중복·무의미 답 제거, 라벨 사진 실측 대조) 221건을 확정했습니다.

```json
{"question": "What is the amount of sugars in 100g of Tomato Ketchup?",
 "gold_fact_ids": ["food-50457250-sugars_100g"],
 "gold_answer": "There are 22.8 g of sugars in 100g of Tomato Ketchup.",
 "tags": {"modality": "image", "lang": "en", "hop": "single"}}
```

이 QA 하나에 전 단계가 응축돼 있습니다. 정답은 ③의 팩트를 가리키고, 그 팩트의 배치(④)가 채점 시 정답 페이지가 되며, counterfactual(⑤)에서는 같은 질문의 답이 13.1g이어야 합니다.

## 재현하기

```bash
# .env (또는 상위 kg_neo4j/.env)에 키 두 개
#   TOUR_API_KEY=...      # 한국관광공사 공공데이터 키
#   OPENAI_API_KEY=...    # gpt-4o-mini (소개글 추출·라벨 대조·QA 초안)

python -m scripts.select_entities food     # 후보 수집·채점
python -m scripts.select_entities place
python -m scripts.extract_facts            # facts.jsonl 생성
python -m scripts.build_fixtures           # entities/ 빌드
python -m scripts.build_fixtures --overrides work/counterfactual-overrides.jsonl   # counterfactual/ 빌드
python -m scripts.validate                 # 무결성 검사 7종
python -m scripts.gen_qa draft             # QA 초안 + 검수용 CSV
python -m scripts.gen_qa finalize          # 검수 반영 → qa.jsonl
```

API 응답과 LLM 호출이 전부 `data/raw/`에 캐시되므로, 캐시가 있으면 네트워크 없이도 동일한 결과가 재생성됩니다.

## 평가 하네스 (예정)

하네스는 doc 04로 설계 예정입니다. 두 층으로 나눕니다 — 파이프라인의 최종 답만 보고 매기는 **블랙박스 점수**(안정적, 골드 기준)와, verbose 로그를 파싱해 "정답 페이지를 검색했는가, VLM을 호출했는가"를 들여다보는 **trace 진단**(베스트에포트, 점수에는 불포함). 이미지 QA 11건은 같은 값이 HTML에도 있으므로, 이미지 활용 능력을 재려면 증거 제한 모드가 필요합니다.

## 출처와 주의사항

- 음식 데이터·사진: [Open Food Facts](https://openfoodfacts.org) (ODbL / CC-BY-SA). 크라우드소싱 데이터라 실세계와 다른 값이 있을 수 있는데, **일부러 교정하지 않았습니다** — 벤치마크의 정답은 실세계가 아니라 이 코퍼스 기준입니다(텍스트 정본 정책). 라벨 사진과 텍스트가 어긋나는 21쌍은 이미지 QA에서 제외해 뒀습니다.
- 장소 데이터·사진: 한국관광공사 TourAPI. 사진은 공공누리 제1유형만 채택했습니다.
- 장소의 GLN(`952...`)은 GS1 데모 프리픽스로 만든 허구 식별자이고, **counterfactual 코퍼스의 값들은 의도적으로 틀린 값입니다.** 이 저장소의 어떤 수치도 실세계 정보로 인용하지 마세요.

## 한계와 TODO

- 이미지 QA는 영양라벨 판독 한 종류, 11건입니다. 정면/성분표 사진과 장소 사진으로 늘릴 여지가 있습니다.
- 멀티홉은 5건뿐입니다 (변조 팩트를 정답에서 빼다 보니 후보가 줄었습니다).
- 페이지 산문을 LLM으로 증량하는 것(슬롯 템플릿 + 오염 스캔)은 설계만 있고 미구현입니다.
- 하네스(doc 04)가 다음 단계입니다.
