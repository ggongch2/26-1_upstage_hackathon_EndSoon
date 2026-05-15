# 데모데이 부스 운영 가이드

> STEM 영어 원서 PDF → 레이아웃·수식·표·이미지가 살아있는 한국어 Word/PDF 번역기.
> Upstage Document Parse + Solar Pro2 기반.

---

## 1. 30초 엘리베이터 피치

> "이공계 영어 원서 PDF를 **레이아웃·수식·표·이미지가 살아있는** 한국어 Word/PDF로 자동 번역합니다. Upstage **Document Parse**가 PDF를 element 단위(문단/제목/표/그림/수식)로 구조 분해해주고, **Solar LLM**이 페이지 컨텍스트 안에서 일관된 용어로 번역합니다. 단순 텍스트 번역기가 아니라 원본 책의 시각적 리듬을 보존하는 게 핵심입니다."

차별점 한 줄: **수식은 Word 네이티브 OMML로, 표는 진짜 Word 표로, 그림은 원본 비율로, 페이지 마진까지 PDF와 동일**.

---

## 2. 시연 흐름 (3~4분)

미리 준비할 PDF: **수식·표·그림이 골고루 있는 30페이지 이하**. 큰 PDF는 시연 시간 안에 안 끝남.

| 시간 | 내용 |
|---|---|
| 0:00–0:15 | **비교 미끼** — 같은 PDF를 Google Translate에 통째 던진 결과 띄우기. 레이아웃 박살, 수식 깨짐 |
| 0:15–0:45 | 우리 서비스에 PDF 드래그앤드롭. 진행 stage(① 업로드 → ⑦ PDF 변환) 보여주기 |
| 0:45–1:15 | Document Parse 결과 강조: `output/{job_id}_parsed.md` 열어서 element 단위 분해 보여주기 (paragraph/heading/equation/table/figure + 페이지 + base64 메타) |
| 1:15–1:55 | **2-Pass 용어집 검토 UI** 등장 — Solar가 추출한 N개 용어, 사용자가 직접 수정/추가 가능 |
| 1:55–3:00 | 번역 진행 (페이지 마커 배치 호출 보여줌) → 끝나면 docx/PDF 다운로드 → 원본 PDF와 나란히 띄워 비교 |

비교 시 강조 포인트:
- 페이지 사이즈/마진이 원본과 동일 (자동 sniff)
- 수식이 Word 네이티브 수식으로 살아있음 (편집 가능)
- 표가 진짜 Word 표 (셀 텍스트만 번역됨)
- 그림이 원본 비율
- 러닝 헤더("198 LINEAR ALGEBRA ...") 자동 제외

---

## 3. 아키텍처 한눈에

```
PDF 업로드
  │
  ├─ [페이지 수 > 90?] → PyMuPDF로 90페이지씩 분할 → 병렬 parse → element 머지
  │
  ▼
Upstage Document Parse
  · output_formats=[text, html, markdown]
  · coordinates=true (← PDF crop fallback에 필요)
  · chart_recognition=true
  · base64_encoding=[figure, chart, table]
  ▼
PyMuPDF로 table/equation 좌표 영역 PNG 크롭 (200 DPI) → element.base64에 첨부
  ▼
Solar 글로서리 추출 (청크 병렬, JSON hard constraint)
  ▼
2-Pass 검토 UI (사용자가 용어 수정)
  ▼
Solar 번역
  · 텍스트 element: ⟦Ek⟧ 마커로 페이지 단위 배치 (호출 수 10~30x 절감)
  · 표 element: HTML 트리 walk로 셀 텍스트만 번역 (base64 있으면 이미지 통과)
  · 수식 element: base64 있으면 이미지, 없으면 LaTeX→MathML→OMML 변환
  · 그림/차트: PASSTHROUGH (base64 그대로)
  · boilerplate 자동 감지로 러닝 헤더 SKIP
  ▼
python-docx 재조립
  · 페이지 사이즈/마진을 원본 PDF에서 sniff (PyMuPDF)
  · 한글/Latin 폰트 분리 + 줄간격 + 단락 간격 + 배경색 환경변수
  · LaTeX → OMML로 Word 네이티브 수식 삽입
  · base64 → PIL → 원본 픽셀/200 DPI = 자연 크기로 삽입
  ▼
docx2pdf (Word COM) 또는 LibreOffice headless로 PDF 변환
```

n8n은 외부 트리거(웹훅) → FastAPI 호출 오케스트레이션 담당.

---

## 4. Upstage API 핵심 활용 (강조 포인트)

### 4.1 Document Parse — 이게 진짜 차별점

다른 OCR/PDF 추출 도구는 "텍스트 한 덩어리" 또는 "페이지 이미지"만 줌. Document Parse는:

| 옵션 | 의미 | 우리 활용 |
|---|---|---|
| `output_formats=[text,html,markdown]` | 같은 element를 세 형식으로 동시 출력 | 본문은 text, 표는 html, 차트는 markdown으로 분기 처리 |
| `coordinates=true` | element별 페이지 정규화 좌표 (x,y) | PyMuPDF로 PDF 영역 잘라 PNG fallback 생성 |
| `chart_recognition=true` | 차트를 markdown 표 형태로도 추가 추출 | base64 없을 때 axis/legend 텍스트라도 살림 |
| `base64_encoding=[figure,chart,table]` | 해당 카테고리는 PNG base64까지 한 번에 반환 | docx 삽입 즉시 가능, 별도 렌더링 불필요 |

**100페이지 제한 우회**: PyMuPDF로 자동 분할 (`pipeline/parser.py:_parse_chunked`).
- 90페이지 단위로 자름 (안전 마진)
- 청크 3개씩 병렬 호출
- 결과 elements 머지 시 page 번호 보정 + ID 재번호
- 259페이지 PDF 검증 완료

### 4.2 Solar Pro2

| 용도 | 프롬프트 / 옵션 |
|---|---|
| 글로서리 추출 | `temperature=0.0`, `response_format={"type":"json_object"}`, `max_tokens=1500` |
| 단일 번역 | `TRANSLATE_SYSTEM_SINGLE` + `<source>...</source>` 태그형 페이로드 |
| 배치 번역 | `TRANSLATE_SYSTEM_BATCH` + `<segments>...</segments>` + `⟦Ek⟧...⟦/Ek⟧` 마커 |
| 글로서리 주입 | `<terminology>{...}</terminology>` JSON 하드 컨스트레인트 |

핵심 설계: **single과 batch 시스템 프롬프트 분리** — "exactly one translation"과 "multiple marker blocks"가 충돌하므로 상속 끊고 독립.

---

## 5. 기술 디테일 — 깊이 들어가면 받을 만한 질문 답변

### 5.1 페이지 단위 마커 배치 번역
- 같은 페이지의 `paragraph/heading/list/caption` element들을 `⟦E0⟧text⟦/E0⟧⟦E1⟧text⟦/E1⟧...`로 묶어 한 번에 Solar 호출
- 페이지당 ~3500자 단위 청크 분할
- **호출 수 10~30배 절감** + 같은 페이지 컨텍스트로 번역 일관성 ↑
- 모델이 마커 누락 시 누락 슬롯만 element 단위 재호출 (`_translate_chunk_individually`)
- `equation/table/figure/chart`는 배치에서 제외 — 기존 element 단위 경로 유지 (구조 보존 로직 살림)

### 5.2 boilerplate 자동 감지 (`pipeline/boilerplate.py`)
- 짧은 텍스트(≤80자)만 대상
- 정규화: lowercase + 공백 축약 + **양쪽 끝의 페이지번호 제거** (`^\d+\s+|\s+\d+$`)
  - 그래서 `"198 Chapter 4"`와 `"199 Chapter 4"`가 같은 정규형으로 매칭
- 동일 정규형이 **3페이지 이상**에 등장하면 SKIP 마킹
- 실제 로그 예시:
  ```
  boilerplate detected (3 pages, ids=[9, 42, 78]): 'Likelihood Ratio / Score Function Policy Gradient'
  translator: 9 boilerplate elements will be dropped
  ```

### 5.3 수식 처리 (이중 안전망)
1. **1차** — Document Parse가 추출한 LaTeX → `latex2mathml.converter`로 MathML → MS의 `MML2OMML.XSL` XSLT 변환 → Word 네이티브 OMML 수식 삽입 (`pipeline/math_render.py`)
2. **2차 fallback** — LaTeX 깨졌거나 변환 실패 시, Document Parse 좌표로 PDF 영역을 PyMuPDF로 200 DPI 크롭 → base64 PNG → 이미지로 삽입
3. 인라인 수식(`$...$`, `\(...\)`)은 번역 단계에서 `_protect_latex`로 placeholder(`⟦M0⟧`) 치환 후 보호 → 번역 끝나고 복원 → `_INLINE_MATH_RE`로 잘라서 OMML 변환

### 5.4 표 처리 (이중 안전망)
1. **1차** — Document Parse HTML → BeautifulSoup 트리 walk → `NavigableString` 셀 텍스트만 번역 → python-docx `add_table()` + `Table Grid` 스타일
2. **2차 fallback** — PDF crop base64가 붙어있으면 셀 번역 스킵하고 이미지로 그대로 삽입 (OCR이 행렬/수식 든 표를 깨뜨릴 때 안전망)

### 5.5 이미지 크기 보존 (`docx_builder.py:_add_image_from_base64`)
- base64 PNG → PIL로 정규화 → `픽셀 / source_dpi = 자연 크기 inch`
- PDF crop은 200 DPI로 렌더하므로 source_dpi=200 기본
- 인라인 수식(540×30px → 2.7"×0.15") vs 풀폭 차트(1200×800px → 6"×4") 자동 구분
- width만 명시, height는 비워 비율 자동 유지
- 최대 6" 폭으로 클램프

### 5.6 페이지 레이아웃 자동 매칭 (`pipeline/page_layout.py`)
- **페이지 사이즈**: `page.rect.width / 72` — PDF MediaBox 메타데이터에서 정확히 추출. 해상도 무관.
- **마진**: PDF 메타데이터에 없으므로 추정
  - `page.get_text("blocks")`로 텍스트 덩어리 좌표 리스트 받음
  - 안쪽 85% 영역에 든 블록만 사용 (페이지번호/러닝헤더 배제)
  - `margin_left = min(block.x0)`, `margin_right = w - max(block.x1)` 등
  - 앞 5페이지 샘플링 후 중앙값으로 안정화 (표지 등 이상치 제외)
  - 0.4" 하한
- 환경변수(`DOCX_PAGE_WIDTH_IN`, `DOCX_MARGIN_*_IN`)로 override 가능

### 5.7 폰트 / 단락 포맷 / 배경색
모두 환경변수로 노출:

| 변수 | 기본 | 효과 |
|---|---|---|
| `DOCX_KOREAN_FONT` | 맑은 고딕 | eastAsia 슬롯 |
| `DOCX_LATIN_FONT` | (empty) | ascii/hAnsi/cs 슬롯. 영문은 Cambria 등 별도 가능 |
| `DOCX_FONT_SIZE` | 11 | 본문 pt |
| `DOCX_LINE_SPACING` | (Word default) | 1.15 등 |
| `DOCX_PARAGRAPH_SPACING_AFTER_PT` | (Word default) | 0~6 |
| `DOCX_FIRST_LINE_INDENT_IN` | (Word default) | 0.25 |
| `DOCX_PAGE_BACKGROUND` | (empty) | `#FAF7F0` 등 6자리 hex |
| `DOCX_BODY_COLOR` | (empty) | 본문 글자색 |

### 5.8 글로서리 (2-Pass)
- **추출** — `parsed.full_text`를 6000자 청크로 자르고 (`GLOSSARY_CHUNK_SIZE`) 최대 3청크까지 (`GLOSSARY_MAX_CHUNKS`) Solar에 병렬 호출 (`ThreadPoolExecutor`, default 4 워커)
- 머지 시 인덱스 순서대로 iterate + `setdefault` → "먼저 등장한 청크 우선" 정책
- 청크별 timeout 90초 (`GLOSSARY_TIMEOUT`), 한 청크 hang이 전체 stage 막는 것 방지
- **검토 UI** — `awaiting_review` stage에서 `threading.Event`로 워커 스레드 블록 → 사용자가 `/jobs/{id}/glossary` POST로 수정본 제출 → 이벤트 set → 번역 재개
- **주입** — `<terminology>{...}</terminology>` JSON 형식으로 system prompt에 강제 (불릿 리스트보다 모델 adherence ↑)
- **후처리** — `glossary.apply()`로 결과 텍스트에서 영어 단어 → 한국어 정규식 치환. 단, 결과가 영어 위주(`_looks_translated()` False)면 적용 안 함 (영어 문장 안에 한국어 끼워넣기 방지)

### 5.9 영어 병기 방지
- 초기 결과 분석: 176 paragraph 중 35개(20%)가 "Figure 4.1: 행 그림 : The point..." 같은 혼합문
- 원인: 모델의 보수적 영어 보존 + glossary.apply()의 부분 정규식 치환
- 해결:
  - 프롬프트의 보존 절을 화이트리스트로 명시 (`LaTeX/code/identifiers/numbers/units/acronyms`만 영어 유지)
  - "Every other English word MUST be translated" 강조
  - `_looks_translated()` 가드로 글로서리 후처리 조건부 적용

### 5.10 디버그 출력
모든 job 처리 후 자동 생성:
- `output/{job_id}_raw.json` — Document Parse 응답 truncated dump
- `output/{job_id}_parsed.jsonl` — element 한 줄씩 (id, page, category, text, has_base64, ...). jq/grep 가능
- `output/{job_id}_parsed.md` — 페이지별로 정리된 사람용 dump (base64는 길이만)
- `output/{job_id}_translated.docx`, `.pdf`

검증할 때 `_parsed.md`만 열면 Document Parse가 뭘 잡았는지 한눈에 보임.

### 5.11 Windows COM 처리
- `docx2pdf`는 Word를 COM으로 호출. COM은 **스레드 단위**라 워커 스레드에서 `pythoncom.CoInitialize()` 필요
- FastAPI 메인 스레드는 자동, 우리가 만든 `threading.Thread` 워커는 직접 호출
- 안 하면 `'CoInitialize가 호출되지 않았습니다'` 에러로 PDF 변환 실패
- `_try_docx2pdf`가 `CoInitialize()` → `convert()` → `CoUninitialize()` 순서로 감쌈

---

## 6. 예상 질문 + 답변

| 질문 | 답변 |
|---|---|
| "ChatGPT한테 PDF 던지면 안 되나?" | PDF 통째로 던지면 레이아웃 모름. 우리는 element 단위 구조를 먼저 알기 때문에 표는 표로, 수식은 수식으로, 그림은 그림으로 보존. ChatGPT 결과는 "텍스트 한 덩어리" |
| "Google Translate / DeepL과 차이?" | 두 서비스도 docx 업로드 지원하지만 (1) 수식이 텍스트로 풀어져 깨짐 (2) 글로서리 강제 안 됨 (3) 200페이지 대용량 처리 안 됨. 시연에서 옆에 띄우면 즉시 차이 보임 |
| "100% 정확?" | 아니오. OCR 깨지면 일부 element 손실. 그래서 (1) 표/수식은 PDF crop 이미지 fallback (2) 사용자가 용어집 직접 검토 (3) `_parsed.md`로 중간 단계 직접 검증 가능 |
| "비용/시간?" | 로그 기준 29페이지 슬라이드 ~15초 (글로서리 5초 + 번역 10초). API 호출: 글로서리 N청크 + 페이지당 1회 + 그림/표 별도 |
| "왜 Word? PDF만 주면 안 되나?" | Word는 편집 가능 — 사람이 다듬을 수 있어야 실무에 쓰임. PDF는 옵션으로 추가 변환 |
| "용어집은 어디서 나옴?" | Solar가 본문 청크에서 자동 추출 + 사용자 직접 추가/수정. 분야별 사전 주입도 추후 가능 |
| "수식 정확도?" | latex2mathml → MS XSLT로 OMML 변환. Word 네이티브라 편집 가능. 변환 실패 시 PDF 영역 200 DPI 크롭 이미지로 자동 fallback |
| "n8n은 왜?" | 외부 트리거(Slack, Google Drive 새 파일, 웹훅)에 연결. 학원이 매주 새 자료 받는 워크플로우, 연구실 논문 자동 번역 큐 등 |
| "왜 Upstage가 핵심?" | 다른 OCR/PDF 추출 도구는 element 분류를 안 해주거나 부정확. Document Parse는 분류 + 좌표 + base64까지 한 번에 → 우리 모든 후속 처리가 이 출력에 의존 |
| "큰 책 (500페이지+)?" | PyMuPDF 자동 분할로 처리. 259페이지 검증 완료. 시간은 페이지 수에 거의 선형 |
| "원본과 100% 똑같이?" | 시각적으로 매우 가깝게 (페이지 사이즈, 마진, 이미지 비율, 수식 OMML). 픽셀 단위 동일은 docx의 한계상 불가 — 그건 LaTeX 같은 다른 포맷이 적합 |

---

## 7. 역할 분담 추천

| 담당 | 할 일 |
|---|---|
| **설명 담당** | 30초 피치 → 문제 → Upstage API 역할 → 시장. 슬라이드 1장 |
| **시연 담당** | 라이브 데모 운영. 미리 띄울 것: 로컬 서버 + Google Translate 비교용 docx + 원본 PDF + 우리 결과 docx + `_parsed.md` |
| **질문 대응** | 위 표 + 섹션 5 디테일. 특히 "마커 배치", "boilerplate 감지", "페이지 레이아웃 자동 추출", "PDF crop fallback" 네 가지를 짧게 짚고 깊은 질문 받으면 들어가는 식 |

---

## 8. 사고 방지 체크리스트

- [ ] 서버 미리 띄우고 한 번 시연 PDF로 처리 후 결과 확인 (Word 첫 실행 지연 + Document Parse 캐시 회피)
- [ ] `MOCK_MODE=false` 확인 (모드 배지가 "LIVE 모드"인지)
- [ ] `git pull` 최신 (CoInitialize fix, 대용량 PDF 분할, 영어 병기 차단, 페이지 레이아웃 sniff 다 포함)
- [ ] Word 설치 + 한 번 띄워서 라이센스 인증 확인 (docx2pdf PDF 변환용). 없으면 LibreOffice
- [ ] PyMuPDF 설치 확인 (`pip install pymupdf`) — PDF crop, 페이지 분할, 페이지 레이아웃 sniff 모두 이게 있어야 함
- [ ] 데모 PDF는 30페이지 이하, 수식·표·그림 골고루
- [ ] 원본 PDF + 우리 출력 docx를 미리 다른 모니터에 띄워두기 (시각적 임팩트)
- [ ] `output/{job_id}_parsed.md` 한 페이지 미리 열어두기 (Document Parse 결과 카드)
- [ ] 환경변수 미리 set (`.env`):
  - `UPSTAGE_API_KEY=...`
  - 폰트 매칭하려면 `DOCX_KOREAN_FONT`, `DOCX_LATIN_FONT`, `DOCX_LINE_SPACING` 등

---

## 9. 코드 위치 빠른 참조

| 기능 | 파일 |
|---|---|
| FastAPI 엔트리포인트 | `main.py` |
| Document Parse 래퍼 + 자동 분할 | `pipeline/parser.py` |
| Solar 클라이언트 | `pipeline/solar.py` |
| 글로서리 추출 + 2-Pass 핸드쉐이크 | `pipeline/glossary.py`, `pipeline/jobs.py` |
| 페이지 단위 마커 배치 번역 | `pipeline/translator.py` |
| boilerplate 감지 | `pipeline/boilerplate.py` |
| PDF 좌표 영역 크롭 | `pipeline/pdf_crop.py` |
| LaTeX → MathML → OMML | `pipeline/math_render.py` |
| docx 재조립 + 페이지 레이아웃 적용 | `pipeline/docx_builder.py` |
| 페이지 사이즈/마진 추출 | `pipeline/page_layout.py` |
| 검토 UI | `static/index.html` |
| n8n 워크플로우 | `n8n/workflow.json` |

---

## 10. 실행 명령어

```powershell
# 서버 띄우기
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload --port 8000

# 브라우저
start http://localhost:8000

# CLI 테스트
curl.exe -X POST http://localhost:8000/translate `
  -F "file=@samples/textbook.pdf" `
  -F "want_pdf=true" `
  -F "interactive=true"
```
