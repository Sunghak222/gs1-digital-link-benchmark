# 의약품 데이터 소스 분석 — openFDA + DailyMed (벤치마크 3번째 축 후보)

> 2026-07-30 작성. 벤치마크 도메인 다양성(식품·장소 → +의약품) 검토 중 후보 데이터 소스를
> API 표본으로 실측한 결과. 탈락 후보(화장품·도서)의 근거 수치도 §6에 보존.
> 관련: [08 대량 확장], [10 OFF 필드-linktype 매핑], docs/16(T1 — §5.4 실제 개정 이력 참고)

## 0. 한 줄 결론

**성립.** GTIN(UPC) 붙은 라벨 27,698개, 장문 텍스트 충실(효능 98%·복용법 95%), 포장 이미지
커버리지 100%(표본), 퍼블릭 도메인. 중복이 심하지만(재포장 업체) 기존 정규화 룰로 정리 가능.

## 1. 데이터가 뭔가

미국 FDA에 제출되는 **의약품 라벨(SPL, Structured Product Labeling)** — 약 상자·설명서에
인쇄되는 내용의 구조화 문서. 두 기관이 같은 원천을 다른 방식으로 제공한다:

| 시스템 | 운영 | 역할 | 형식 |
|---|---|---|---|
| **openFDA** | FDA | 라벨 텍스트를 JSON 검색 API로 제공 | JSON (검색·집계 쿼리) |
| **DailyMed** | NIH/NLM | 라벨 원문 열람 + **포장 이미지** + **개정 이력** | XML/JSON + 미디어 |

두 시스템은 `set_id`(라벨 묶음 고유번호)로 연결된다. openFDA 검색 결과의
`openfda.spl_set_id`로 DailyMed의 이미지·이력을 바로 조회할 수 있다.

라이선스: 미 연방정부 저작물 = **퍼블릭 도메인** (이미지 포함). 재배포 제약 없음.

## 2. 규모와 GTIN 연결 (2026-07-30 덤프 전수 실측)

```
전체 라벨:                261,077   (덤프 14파트 1.8GB, export 2026-07-29)
openfda.upc 보유 라벨:     27,698   ← 우리가 쓸 풀 (UPC/EAN = GTIN-12/13)
GTIN 체크섬 유효:          27,698   (100% — 검증 손실 0)
창고:                     work/bulk/pharma-raw-matched.jsonl.gz (231MB, 원본 전체 보존)
```

스캔 도구: `python -m scripts.bulk.pharma_harvest scan` (덤프 전용, API 0회).

- `openfda.upc` 필드에 실물 바코드가 들어 있어 **시뮬레이션이 아닌 실제 GTIN**으로
  `01/{gtin}` DL URI를 만들 수 있다 — 식품(OFF 바코드)과 같은 패턴.
- GS1 서사: 의료는 GS1의 핵심 버티컬(의약품 유통 바코드 의무화 — 미 DSCSA, EU FMD).
  "규제가 실존하는 도메인"이라 논문 서사가 식품보다도 강하다.

## 3. 필드 충실도 (UPC 보유 27,698개 전수 실측, 2026-07-30)

| 필드 | 채움(전수) | linktype 대응(초안) |
|---|---|---|
| `indications_and_usage` 효능·용법 | 100% (27,594) | pip |
| `dosage_and_administration` 복용법 | 100% (27,582) | instructions |
| `warnings` 경고 | 73% (20,314) | safetyInfo |
| `active_ingredient` 유효 성분 | 58% (16,055) | ingredientsInfo |
| `inactive_ingredient` 비활성 성분 | 58% (16,164) | ingredientsInfo |
| `purpose` 용도 | 58% (16,122) | pip |
| `questions` 문의처 | 36% (10,081) | support류 |
| `storage_and_handling` 보관법 | 28% (7,733) | storage류 |

- 효능·복용법은 사실상 전 라벨 보유 — 페이지 2장은 무조건 나온다. 표본 40개 기준
  **300자 이상 장문 필드 평균 1.4개/라벨** — 도서(설명문 8%)와 달리 본문이 실제로 있다.
- 제품 유형 전수: 일반의약품(OTC) 16,097 : 처방약 11,597 (+세포치료제 4 — 제외 대상).
- 구조화 필드 추가: `openfda.brand_name / generic_name / manufacturer_name / route /
  substance_name / product_ndc / product_type(처방/일반)` — 표본 구성은 일반의약품(OTC) 24 :
  처방약 16.
- 멀티홉 축: ① 제조사 ② **동일 성분 다른 브랜드(제네릭 관계)** — 성분 클러스터가 굵다
  (ACETAMINOPHEN 라벨 348개, ZINC OXIDE 604개 등). 식품의 브랜드 클러스터보다 강한 축.

## 4. 이미지 (DailyMed, 표본 30 set_id 실측)

- **이미지 1장 이상: 30/30 (100%)** — 분포는 1장이 다수(22/30), 최대 7장.
- 이미지 정체가 **포장/라벨 스캔**이라 식품의 영양라벨 사진과 같은 역할 —
  "라벨 사진에서 용량 읽기" 이미지 QA에 적합.
- 단, 식품의 "이미지 3장" 검수 기준은 도메인별 기준으로 완화 필요(의약품 1장+).

## 5. 중복 구조 (실측) 와 정리 규칙 초안

풀 27,698개는 라벨 단위라 같은 약이 여러 번 등장한다 (전수 실측):

- **브랜드+성분 정규화 키로 15,130종** — 라벨 1.8개당 1종 꼴. 중복이 심하다는 표본
  관찰은 맞았지만 전수로는 종 수가 넉넉하다.
- 브랜드명 "Ibuprofen" 라벨 213개(제조사·함량·포장별 별도 등록).
- **최대 노이즈 = 재포장 업체**: 남의 약을 소분 재포장하며 라벨을 복제 등록 —
  제조사 상위가 전부 이들이다 (정규화 후 NuCare 1,306 / Proficient Rx 1,104 / PD-Rx 638).
  정규화 룰이 표기 흔들림("NuCare ,Inc." vs ", Inc.")을 실제로 병합함을 전수에서 확인.
- 진짜 제조사 상위: Kenvue 365, P&G 335 등 — 재포장 업체를 걷어내면 정상 분포.

**정리 규칙 초안**: `브랜드+성분+함량` 키로 대표 1개 선정 + 재포장 업체 제외.
OFF의 `브랜드+제품명` 중복 제거에 키 하나 추가한 수준. 15,130종 기준, 정리 후에도
수천 개 단위 확실 (목표 수백~2천에 충분).
재포장 제외는 이름 정규화가 아니라 **`openfda.is_original_packager` 필드(85% 채움)로
직접 판별 가능** — 부록 A.2 전수 집계에서 확인 (2026-07-30 추가).

### 5.4 T1(낡음 실험)과의 접점 — 개정 이력이 실물로 있다

DailyMed의 `/history` API가 라벨의 **실제 개정 이력**(버전·개정일)을 제공한다.
우리 T1 실험은 코퍼스를 인공 변조하는데, 의약품 축은 "실제로 문서가 어떻게 바뀌는가"의
현실 표본을 덤으로 준다 — 변조 시나리오의 생태 타당성 방어 근거로 쓸 수 있다.

## 6. 탈락 후보 근거 (같은 날 실측)

| 후보 | 수치 | 탈락 사유 |
|---|---|---|
| Open Beauty Facts | 총 67,420개 / 성분 완료 28% / 사진 검증 4% / 둘 다 3%(2,061) | 풀 부족 + 상위 제품도 필드 오염(빈 제품명, 쓰레기 카테고리) + 영양표 없음 |
| Open Library (도서) | 게이트(제목+저자+출판사+ISBN+표지+주제) 14%로 풀은 백만 단위지만, **설명문 8%·발췌 0%** | 장문 콘텐츠 부재 — 팩트 8~10개 + 표지 1장뿐이라 페이지를 못 채움 |
| Google Local | — | 장소 중복 + **약관상 저장·재배포 금지** (공개 벤치마크 불가) |
| 섬유 DPP | 공개 데이터 없음 (규제 시행 전) | 탈락 아님 — 수작업 소량(수십 개) "서사용 고명"으로 유지 |

## 7. 리콜 데이터 (`*/enforcement`) — 새 축이 아니라 기존 축 보강 재료

규모(2026-07-30 실측): 식품 29,264 / 의약품 17,832 / 기기 39,588. 같은 스키마.
식품 기준 `code_info`에 UPC 언급 3,030건(10%), 진행 중(Ongoing) 상태 1,067건.

**스키마 성격 — 사실상 전 필드가 문자열** (날짜도 "20160808", 수량도 "1,990 bottles"):

| 그룹 | 필드 | 비고 |
|---|---|---|
| 제품 | `product_description`(장문), `code_info`, `product_quantity` | code_info는 UPC·로트·유통기한이 **문장에 섞인 자유 텍스트** |
| 사유·등급 | `reason_for_recall`(장문), `classification` | Class I(사망 위험)/II/III |
| 회사·유통 | `recalling_firm`+주소 5필드, `distribution_pattern` | 유통 지역도 자유 텍스트 |
| 절차·시간 | `status`, 날짜 4종, `voluntary_mandated`, `recall_number` 등 | **status는 Ongoing→Completed→Terminated로 바뀌는 상태값** |

**용도 판정 (7-30 사용자 논의)**: 구조화 작업 불필요 — 오히려 자유 텍스트가 이점.
- 서술문·code_info 덩어리 → **recallStatus linktype 페이지 본문** — 현실의 지저분한
  문서를 재현해 검색 난이도(CRAG "검색 현실성")를 공짜로 올린다.
- 정답표(gold)에 쓸 값(status·classification·날짜·회사)은 **이미 분리된 필드** —
  "페이지는 지저분하게, 정답은 깨끗하게"가 한 레코드에 다 있다.
- 필요한 가공은 하나: `code_info`에서 UPC 정규식 추출 (우리 엔티티와의 매칭용).

**T1(낡음 실험) 소재**: status 상태 전이가 실물로 존재 — Ongoing 1,067건,
2016 개시→2024 종결(8년 뒤 상태 변경) 사례 실측. "문서는 진짜로 늦게 바뀐다"의 근거.

**유의**: UPC 커버리지 10%라 우리 식품 3,500개와의 교집합은 작을 수 있음 — 교집합
실측 후, 적으면 형식만 실데이터에서 빌리고 내용은 시뮬레이션이 현실적.

## 8. API 목록

### openFDA (`https://api.fda.gov`) — 텍스트·검색

| 엔드포인트 | 내용 | 우리 용도 |
|---|---|---|
| `GET /drug/label.json` | SPL 라벨 검색 | **주력** — 수확 파이프라인의 원천 |
| `GET /drug/enforcement.json` | 리콜 정보 | recallStatus 소재 — **§7 분석 완료** |
| `GET /drug/ndc.json` | NDC(미국 의약품 코드) 사전 | 포장·코드 대조 |
| `GET /drug/event.json` | 부작용 신고 | (범위 밖) |
| `GET /drug/drugsfda.json` | 허가 정보 | (범위 밖) |

참고 — openFDA 전 분류 21개 데이터셋 규모(2026-07-30 실측): 사건 신고류가 최대
(기기 사고 2,537만·의약품 부작용 2,033만·동물 136만 — 전부 범위 밖), 행정·사전류
(허가·등록·코드)가 나머지. 페이지 본문형은 Drug > Label 유일. 추가 검토 후보:
- `device/udi.json` (5,083,948건): 의료기기 UDI — 발급기관이 GS1이면 식별번호가 곧
  GTIN. "실물 GTIN 4번째 축" 후보이나 장문·이미지 부재 예상 (착수 전 충실도 체크 필요).
- `*/enforcement.json`(리콜): §7에서 분석 완료 — 기존 축 보강용.

쿼리 문법: `search=필드:값`, `_exists_:필드`, `count=필드.exact`(집계), `limit`(최대 1000쪽당?
실측 기준 label은 limit≤100 안전), `skip`(최대 25,000 — 그 이상은 400 에러 실측,
전량 수확은 아래 벌크 덤프 사용).
API 키: 없이 분당 40회/일 1,000회, **무료 키 발급 시 분당 240회/일 120,000회**.
벌크 덤프: `https://open.fda.gov/apis/downloads/` — 전체 라벨 JSON 통파일
(OFF 덤프 워크플로와 동일 패턴 재사용 가능).

### DailyMed (`https://dailymed.nlm.nih.gov/dailymed/services/v2`) — 이미지·이력·원문

| 엔드포인트 | 내용 | 우리 용도 |
|---|---|---|
| `GET /spls.json` | 라벨 목록 검색 | 보조 |
| `GET /spls/{setid}.xml` | 라벨 원문(SPL XML) | 텍스트는 openFDA로 충분, 예비 |
| `GET /spls/{setid}/media.json` | **포장 이미지 목록** | **이미지 수확** |
| `GET /spls/{setid}/history.json` | **개정 이력(버전·날짜)** | **T1 생태 타당성** (§5.4) |
| `GET /spls/{setid}/ndcs.json` | 해당 라벨의 NDC들 | 중복 정리 보조 |
| `GET /spls/{setid}/packaging.json` | 포장 단위 정보 | 보조 |
| `GET /drugnames.json`, `/ndcs.json` | 이름/코드 사전 | 보조 |

벌크: DailyMed도 전체 릴리스 ZIP 제공(라벨+이미지 통파일) — 이미지 대량 수확 시 사용.

문서: openFDA https://open.fda.gov/apis/drug/label/ · DailyMed
https://dailymed.nlm.nih.gov/dailymed/app-support-web-services.cfm
필드 사전: https://open.fda.gov/data/datadictionary — Drug > Label 선택 시 라벨 전체
필드의 이름·타입·정의 목록(XLS 내보내기 가능). **의약품용 extract_facts 설계 시 이걸
참조** — 창고 역공학 불필요. (전 분류 지도이기도 함: Drug/Device/Food/Animal/Tobacco ×
데이터셋 — 라벨형 데이터셋은 Drug > Label 유일, 나머지는 사건 기록·코드 사전.)

## 9. 다음 단계

1. ~~벌크 덤프 다운로드 + 전수 스캔~~ — **완료 (2026-07-30)**: 덤프 14파트(1.8GB,
   `data/dump/openfda-label/`) → `scripts/bulk/pharma_harvest.py scan` → 창고
   `work/bulk/pharma-raw-matched.jsonl.gz` (27,698 라벨, 231MB). §2·§3·§5 수치가 전수 기준.
2. docs/17(QA 생성 설계)의 "코퍼스 확장 계획" 절에 본 문서 §0·§3 수치 인용
2-1. 리콜 UPC 교집합 실측 (§7): food/drug enforcement의 code_info UPC ↔ 우리
   엔티티 GTIN — recallStatus 페이지를 실데이터로 할지 시뮬레이션할지의 관문
3. 착수 결정 시: pharma_harvest에 select 단계 추가 (게이트 = 효능+복용법+경고+이미지 생존,
   중복 규칙 §5, 스냅샷 봉투는 OFF와 다른 도메인 스키마라 extract_facts 확장 필요)
4. 중복 정리 규칙(§5) 구현은 브랜드 정규화 모듈과 공유
5. 미검증 잔여: DailyMed 이미지 라이선스 표기 관행(퍼블릭 도메인이나 파일별 확인),
   함량(strength) 필드의 위치 — openfda에 없어 라벨 본문·NDC 사전에서 추출 필요

---

## 부록 A — 라벨 필드 전수표와 사용 계획 (한국어)

> 2026-07-30 창고 27,698개 라벨 전수 집계. 본문 섹션 146종(표 버전 57종 포함) +
> openfda 메타 21종. 범례: ✓ 사용 확정 제안 / △ 후보·보조 / ✗ 미사용.
> extract_facts(의약품) 설계 시 체크리스트로 사용.

### A.1 라벨 본문 섹션

| 필드 | 채움 | 설명 | 사용 |
|---|---|---|---|
| `indications_and_usage` | 100% | 효능·용법 — 무엇에 쓰는 약인가 | ✓ pip |
| `dosage_and_administration` | 100% | 복용법 — 용량·횟수·방법 | ✓ instructions |
| `package_label_principal_display_panel` | 100% | 포장 겉면 인쇄 문구 전체 | ✓ 이미지 QA 교차검증·pip 보조 |
| `spl_product_data_elements` | 100% | 성분·제형 기계 나열(중복 많음) | △ 함량(strength) 추출 후보 |
| `set_id` | 100% | 라벨 묶음 ID | ✓ DailyMed 이미지·이력 연결 키 |
| `effective_time` / `version` | 100% | 라벨 발효일 / 개정 번호 | ✓ 팩트(개정일)·T1 |
| `warnings` | 73% | 경고 (일반의약품용) | ✓ safetyInfo |
| `inactive_ingredient` | 58% | 비활성 성분(부형제) | ✓ ingredientsInfo |
| `active_ingredient` | 58% | 유효 성분+함량 | ✓ ingredientsInfo·함량 |
| `purpose` | 58% | 용도(해열제 등 한 줄) | ✓ pip |
| `keep_out_of_reach_of_children` | 58% | 어린이 주의 상용구 | △ safetyInfo 병합 |
| `description` | 43% | 제형·화학 설명(처방약 위주) | △ |
| `how_supplied` | 42% | 공급 형태(포장 단위·코드) | △ 포장 팩트 후보 |
| `questions` | 36% | 문의처(전화·시간) | ✓ support류 |
| `stop_use` | 36% | 복용 중단 조건 | △ safetyInfo 병합 |
| `when_using` `do_not_use` `ask_doctor(+or_pharmacist)` `pregnancy_or_breast_feeding` | 29~21% | OTC 경고문 하위 항목들 | △ safetyInfo 병합 |
| `storage_and_handling` | 28% | 보관법 | ✓ storage류 |
| `dosage_forms_and_strengths` | 25% | 제형·함량 | △ 함량 추출 후보 |
| `warnings_and_cautions` | 25% | 경고(처방약판) | △ warnings 부재 시 대체 |
| `boxed_warning` | 15% | 최고 등급 경고(블랙박스) | △ safetyInfo |
| `recent_major_changes` | 13% | 최근 라벨 변경 목록 | △ T1(낡음) 소재 |
| `adverse_reactions` `contraindications` `drug_interactions` `overdosage` `pregnancy` `pediatric_use` `geriatric_use` `information_for_patients` 등 | 42~17% | 처방약 전문정보 섹션군(의료진용) | ✗ 범위 밖 |
| `clinical_pharmacology` `pharmacokinetics` `mechanism_of_action` `clinical_studies` `nonclinical_toxicology` 등 | 41~5% | 약리·임상시험 연구 섹션군 | ✗ |
| `spl_unclassified_section` | 40% | 미분류 잡록 | ✗ |
| `*_table` 57종 | ≤23% | 위 섹션들의 표(HTML) 버전 | △ 페이지 렌더 시 본문 합류 |
| 꼬리 40여 종 (<5%) | — | 기기 지침·수의학 등 | ✗ |

### A.2 `openfda` 메타 블록

| 필드 | 채움 | 설명 | 사용 |
|---|---|---|---|
| `upc` | 100% | 실물 바코드(GTIN) | ✓ 엔티티 ID (DL URI) |
| `brand_name` | 100% | 브랜드명 | ✓ 팩트+중복 키 |
| `generic_name` | 100% | 성분명(일반명) | ✓ 팩트+제네릭 멀티홉 축 |
| `manufacturer_name` | 100% | 제조·판매사 | ✓ 팩트+제조사 멀티홉 축 |
| `product_type` | 100% | OTC/처방약 구분 | ✓ 필터·팩트 |
| `route` | 99% | 투여 경로(경구·외용…) | ✓ 팩트 |
| `substance_name` | 98% | 표준 성분명 | ✓ 성분 정규화 보조 |
| `is_original_packager` | 85% | 원제조 vs 재포장 여부 | ✓ **재포장 필터 — 이름 정규화 불필요** |
| `pharm_class_epc` | 30% | 약효 분류(항히스타민제 등) | △ 카테고리 층화 후보 (보완 필요) |
| `spl_set_id` / `spl_id` | 100% | set_id 사본 | △ |
| `product_ndc` / `package_ndc` | 100% | 미국 의약품 코드 | △ 함량 조회·중복 보조 |
| `application_number` | 91% | FDA 허가번호 | ✗ |
| `rxcui` `unii` `nui` 기타 `pharm_class_*` | ≤78% | 외부 표준 코드들 | ✗ |

## Appendix A (EN) — Label Field Census & Usage Plan

> Full-warehouse census over all 27,698 UPC-bearing labels (2026-07-30).
> 146 body sections (incl. 57 `*_table` variants) + 21 `openfda` meta fields.
> Legend: ✓ planned use / △ candidate·auxiliary / ✗ not used.

### A.1 Label body sections

| Field | Fill | Description | Use |
|---|---|---|---|
| `indications_and_usage` | 100% | What the drug is for | ✓ pip |
| `dosage_and_administration` | 100% | Dose, frequency, method | ✓ instructions |
| `package_label_principal_display_panel` | 100% | Full text printed on the package | ✓ image-QA cross-check, pip aux |
| `spl_product_data_elements` | 100% | Machine list of ingredients/forms (noisy) | △ strength extraction candidate |
| `set_id` | 100% | Label-set ID | ✓ join key to DailyMed images/history |
| `effective_time` / `version` | 100% | Label effective date / revision no. | ✓ fact (revision date), T1 |
| `warnings` | 73% | Warnings (OTC style) | ✓ safetyInfo |
| `inactive_ingredient` | 58% | Inactive ingredients (excipients) | ✓ ingredientsInfo |
| `active_ingredient` | 58% | Active ingredients + strength | ✓ ingredientsInfo, strength |
| `purpose` | 58% | One-line purpose (antacid, …) | ✓ pip |
| `keep_out_of_reach_of_children` | 58% | Standard child-safety phrase | △ merge into safetyInfo |
| `description` | 43% | Dosage-form/chemistry description (Rx) | △ |
| `how_supplied` | 42% | Supply format (package units, codes) | △ packaging-fact candidate |
| `questions` | 36% | Contact info (phone, hours) | ✓ support-type |
| `stop_use` | 36% | When to stop using | △ merge into safetyInfo |
| `when_using` `do_not_use` `ask_doctor(+or_pharmacist)` `pregnancy_or_breast_feeding` | 29–21% | OTC warning sub-sections | △ merge into safetyInfo |
| `storage_and_handling` | 28% | Storage instructions | ✓ storage-type |
| `dosage_forms_and_strengths` | 25% | Forms & strengths | △ strength extraction candidate |
| `warnings_and_cautions` | 25% | Warnings (Rx style) | △ fallback when `warnings` absent |
| `boxed_warning` | 15% | Highest-severity (black-box) warning | △ safetyInfo |
| `recent_major_changes` | 13% | Recent label changes | △ T1 (staleness) material |
| `adverse_reactions` `contraindications` `drug_interactions` `overdosage` `pregnancy` `pediatric_use` `geriatric_use` `information_for_patients` … | 42–17% | Rx professional-info sections (for clinicians) | ✗ out of scope |
| `clinical_pharmacology` `pharmacokinetics` `mechanism_of_action` `clinical_studies` `nonclinical_toxicology` … | 41–5% | Pharmacology/clinical-study sections | ✗ |
| `spl_unclassified_section` | 40% | Unclassified leftovers | ✗ |
| 57 `*_table` variants | ≤23% | HTML-table renderings of the above | △ merged into page rendering |
| ~40 tail fields (<5%) | — | Device instructions, veterinary, etc. | ✗ |

### A.2 `openfda` meta block

| Field | Fill | Description | Use |
|---|---|---|---|
| `upc` | 100% | Real barcode (GTIN) | ✓ entity ID (DL URI) |
| `brand_name` | 100% | Brand name | ✓ fact + dedup key |
| `generic_name` | 100% | Generic (ingredient) name | ✓ fact + generic multi-hop axis |
| `manufacturer_name` | 100% | Manufacturer/labeler | ✓ fact + manufacturer multi-hop axis |
| `product_type` | 100% | OTC vs prescription | ✓ filter, fact |
| `route` | 99% | Route of administration | ✓ fact |
| `substance_name` | 98% | Standardized substance name | ✓ ingredient normalization aid |
| `is_original_packager` | 85% | Original manufacturer vs repackager | ✓ **repackager filter — no name-normalization needed** |
| `pharm_class_epc` | 30% | Pharmacologic class (antihistamine, …) | △ category-stratification candidate |
| `spl_set_id` / `spl_id` | 100% | Copies of set_id | △ |
| `product_ndc` / `package_ndc` | 100% | US drug codes | △ strength lookup, dedup aid |
| `application_number` | 91% | FDA approval number | ✗ |
| `rxcui` `unii` `nui` other `pharm_class_*` | ≤78% | External code systems | ✗ |
