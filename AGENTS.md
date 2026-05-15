# AGENTS.md

이 파일은 이 저장소에서 작업하는 Codex/에이전트가 먼저 읽어야 할 프로젝트 안내서다.
세부 내용은 기존 문서를 참조하고, 여기에는 빠른 방향 잡기에 필요한 맥락만 둔다.

## 프로젝트 한 줄 요약

영어 원서 PDF를 Upstage API로 구조 분석하고, Solar LLM으로 한국어 번역한 뒤, 레이아웃을 최대한 보존한 DOCX/PDF 또는 Notion/Markdown 결과물로 재구성하는 서비스다.

## 현재 주력 코드

- Python/FastAPI 앱이 현재 코드베이스의 주력 구현이다.
- 진입점은 `main.py`다.
- 웹 UI는 `static/index.html`이다.
- 핵심 파이프라인은 `pipeline/` 아래에 있다.
- 현재 주 산출물은 DOCX이며, 환경에 따라 PDF 변환도 시도한다.
- n8n 기반 Notion 데모는 별도 실험 구현이며, 현재 Python 코드와 직접 연결되어 있지는 않다.

## 먼저 읽을 문서

작업 전에 목적에 맞게 아래 문서를 확인한다.

- `README.md`: 현재 앱 실행 방법, 전체 파이프라인, 주요 모듈 구조.
- `SUCCESS_CRITERIA.md`: 앞으로 개선할 때의 성공 기준과 구현 우선순위.
- `trial.md`: n8n/Notion 데모에서 얻은 설계 교훈과 실패 사례.
- `Upstage Demo.json`: n8n 실험 워크플로우 원본. Python 코드와 별도 구현이다.

## 현재 Python 파이프라인

현재 흐름은 대략 다음과 같다.

```text
PDF 업로드
-> Upstage Document Parse
-> 표/수식 영역 PDF crop 보강
-> raw debug JSON 저장
-> Solar 기반 glossary 추출
-> 요소별 번역
-> python-docx로 DOCX 재조립
-> 선택 시 PDF 변환
```

주요 파일:

- `pipeline/parser.py`: Upstage Document Parse 래퍼.
- `pipeline/solar.py`: Solar Chat Completions 클라이언트.
- `pipeline/glossary.py`: 전문 용어 glossary 추출.
- `pipeline/translator.py`: 요소별 번역, LaTeX 보호, 표 HTML 텍스트 번역, 중복/잡음 제거.
- `pipeline/docx_builder.py`: 번역 요소를 Word 문서로 재조립.
- `pipeline/pdf_crop.py`: 원본 PDF 좌표 기반 이미지 crop.
- `pipeline/jobs.py`: in-memory job progress registry.
- `pipeline/mocks.py`: `MOCK_MODE=true`일 때 쓰는 오프라인 테스트 구현.

## n8n 데모에서 배울 점

`trial.md`와 `Upstage Demo.json`은 Python 코드에 그대로 붙일 구현이 아니라, 설계 교훈으로 참고한다.

중요한 교훈:

- LLM에게 전체 레이아웃 재구성을 맡기지 않는다.
- 코드는 `layoutBlocks`, `textUnits`, `visualAssets` 같은 구조 정보를 유지한다.
- LLM은 번역해야 할 텍스트 단위만 처리한다.
- 복잡한 표/차트/그림은 텍스트로 억지 복원하지 말고 이미지 우선으로 보존한다.
- 긴 문서는 번역 batch와 결과 merge가 필요하다.
- 결과물에는 누락/실패/검증 메타데이터가 있어야 디버깅이 쉽다.

## 개선 우선순위

현재 합의된 1차 개선 범위는 `SUCCESS_CRITERIA.md`의 P0/P1이다.

우선순위:

1. 기존 FastAPI/DOCX 흐름을 깨지 않는다.
2. `/translate`에 `preferred_terms` 입력을 추가한다.
3. 사용자 선호 용어를 자동 glossary에 merge한다.
4. `translation_meta`를 수집해 job 결과와 summary JSON에 포함한다.
5. 사람이 읽기 쉬운 summary debug JSON을 생성한다.
6. 그 다음 Markdown/Notion exporter를 검토한다.

## 실행 방법 요약

자세한 실행 방법은 `README.md`를 따른다.

기본 로컬 실행:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python -m uvicorn main:app --reload --port 8000
```

브라우저:

```text
http://localhost:8000
```

API 키 없이 테스트하려면 `.env`에서 `MOCK_MODE=true`를 사용한다.

## 환경변수와 비밀값

- `.env`에는 API key가 들어갈 수 있으므로 절대 커밋하지 않는다.
- `.env.example`에는 실제 키를 넣지 않는다.
- Upstage 관련 기본값은 `.env.example`을 참고한다.
- Notion/Cloudinary 관련 값은 아직 Python 주력 코드에는 필수 사항이 아니다.

## 작업 시 주의할 점

- 기존 사용자 변경사항을 되돌리지 않는다.
- `.env`, output 산출물, 사용자가 추가한 PDF/JSON 파일을 임의로 삭제하지 않는다.
- n8n 워크플로우와 Python 앱을 같은 구현으로 착각하지 않는다.
- `trial.md`의 배치 번역 설명은 중요한 방향성이지만, `Upstage Demo.json`에 모두 완성되어 있다고 가정하지 않는다.
- DOCX/PDF 재조립 품질은 현재 Python 코드의 강점이므로 리팩터링 시 회귀 테스트를 우선한다.
- 복잡한 표/수식은 텍스트 복원보다 이미지 보존이 더 안정적일 수 있다.
- 번역 프롬프트를 수정할 때는 LaTeX, 숫자, 식 번호, 전문 용어 보존 규칙을 유지한다.

## 완료 기준

작업 완료 여부는 `SUCCESS_CRITERIA.md`를 기준으로 판단한다.
특히 P0와 P1이 모두 만족되면 1차 코드 개선은 완료로 본다.
