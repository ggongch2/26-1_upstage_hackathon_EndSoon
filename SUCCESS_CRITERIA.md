# 성공 기준: Upstage 기반 영어 원서 한국어 변환 서비스

이 문서는 현재 Python/FastAPI 코드베이스를 개선할 때 사용할 성공 기준을 정리한다.
목표는 n8n 데모에서 얻은 설계 교훈을 코드에 반영하되, 기존 DOCX/PDF 변환 흐름을 깨지 않도록 하는 것이다.

## 최종 목표

영어 원서 PDF를 업로드하면, 사용자가 지정한 용어를 반영해 한국어 DOCX/PDF를 만들고, 표/수식/그림은 가능한 한 이미지로 보존하며, 처리 결과와 누락/실패 여부를 명확히 확인할 수 있다.

## P0: 기존 기능 안정성

현재 기능이 깨지지 않는 것이 가장 우선이다.

- `MOCK_MODE=true`에서 서버 실행, PDF 업로드, 진행상황 표시, DOCX 생성이 끝까지 완료된다.
- `MOCK_MODE=false`에서 유효한 Upstage API key로 작은 영어 PDF 1개를 처리하면 job 상태가 `done`이 된다.
- 기존 API가 그대로 동작한다.
  - `POST /translate`
  - `GET /jobs/{job_id}/progress`
  - `GET /download/{name}`
- 처리 중 예외가 발생해도 서버 프로세스가 죽지 않는다.
- 실패한 작업은 job 상태가 `error`로 남고, 원인을 확인할 수 있는 에러 메시지가 포함된다.
- 기존 DOCX/PDF 출력 경로와 파일명 규칙이 유지된다.

## P1: n8n 데모에서 배운 구조 반영

우선 적용할 핵심 개선 범위다.

### 사용자 선호 용어

- `/translate`가 `preferred_terms` 입력을 받을 수 있다.
- 입력 형식은 다음과 같은 단순 텍스트를 지원한다.

```text
eigenvalue=고윳값
eigenvector=고유벡터
linear transformation=선형변환
```

- 사용자 선호 용어는 자동 추출 glossary에 merge된다.
- 같은 영어 용어가 자동 추출과 사용자 입력에 동시에 있으면 사용자 입력을 우선한다.
- 최종 번역 프롬프트와 후처리 모두에서 선호 용어가 반영된다.

### 처리 메타데이터

작업 완료 결과에 `translation_meta`를 포함한다.

필수 항목:

- 전체 요소 수
- 번역 대상 요소 수
- 번역 완료 요소 수
- 스킵된 요소 수
- 스킵 사유별 카운트
  - header/footer/page number
  - duplicate
  - OCR garbage
  - passthrough equation/figure/chart
- 이미지로 보존된 요소 수
  - table
  - equation
  - figure/chart
- fallback 처리된 요소 수
- 번역 실패 후 원문 유지된 요소 수
- glossary 항목 수
- 사용자 선호 용어 반영 수

### 디버그 산출물

- 기존 raw debug JSON은 유지한다.
- 사람이 빠르게 확인할 수 있는 summary JSON을 추가로 생성한다.
- summary JSON에는 최소한 다음 정보가 들어간다.
  - job id
  - 입력 파일명
  - stage별 처리 결과
  - category별 요소 수
  - `translation_meta`
  - 출력 파일 경로
  - 경고 목록

## P2: Notion/Markdown 출력으로 확장 가능한 구조

P1 이후의 확장 기준이다.

### 중간 표현 분리

파이프라인 내부에서 다음 개념을 명확히 분리할 수 있어야 한다.

- `layout_blocks`: 문서 순서, 블록 타입, 원본 위치/페이지 정보
- `text_units`: Solar가 번역해야 하는 텍스트 단위
- `visual_assets`: 표, 그림, 차트, 수식 등 이미지로 보존할 수 있는 자산

이 구조는 DOCX 빌더와 Markdown/Notion exporter가 함께 사용할 수 있어야 한다.

### Markdown 출력

- Notion API 없이도 Markdown 파일을 생성할 수 있다.
- Markdown에는 다음 내용이 포함된다.
  - 제목
  - 처리 요약
  - glossary 표
  - 한국어 재구성 본문
  - 이미지 보존 실패 시 fallback 메시지
- Markdown 출력 경로가 job 결과에 포함된다.

### Notion 출력

- Notion 관련 환경변수가 없으면 Notion 단계는 자동으로 건너뛴다.
- Notion 관련 환경변수가 있으면 Notion 페이지 생성을 시도한다.
- 성공 시 Notion page URL이 job 결과에 포함된다.
- 실패 시 전체 작업을 실패시키지 않고, Notion 단계의 에러를 warning으로 기록한다.

필요 환경변수:

```env
NOTION_API_KEY=
NOTION_PARENT_PAGE_ID=
```

이미지 외부 호스팅이 필요한 경우:

```env
CLOUDINARY_CLOUD_NAME=
CLOUDINARY_UNSIGNED_UPLOAD_PRESET=
```

## 데모 성공 기준

데모에서 다음 시나리오가 성공하면 충분하다.

1. 사용자가 영어 원서 PDF를 업로드한다.
2. 선택적으로 선호 번역어를 입력한다.
3. 진행상황 UI에서 parse, glossary, translate, docx/pdf 단계를 확인할 수 있다.
4. 작업 완료 후 DOCX를 다운로드할 수 있다.
5. PDF 변환 도구가 설치되어 있으면 PDF도 다운로드할 수 있다.
6. 결과 화면 또는 progress API에서 glossary와 `translation_meta`를 확인할 수 있다.
7. 표/수식/그림이 가능한 한 이미지로 보존된다.
8. 실패하거나 누락된 항목이 있으면 summary JSON 또는 job 결과에서 확인할 수 있다.

## 구현 우선순위

1. P0 회귀 방지 확인
2. `preferred_terms` 입력 추가
3. glossary merge 로직 추가
4. `translation_meta` 수집 구조 추가
5. summary JSON 생성
6. 웹 UI에 선호 용어 입력란과 메타데이터 요약 표시
7. Markdown exporter 추가
8. Notion exporter 추가

## 완료 판단

P0와 P1이 모두 만족되면 이번 코드 개선의 1차 목표는 완료로 본다.
P2는 Notion 데모와 Python 코드베이스를 통합하는 다음 단계의 성공 기준으로 둔다.
