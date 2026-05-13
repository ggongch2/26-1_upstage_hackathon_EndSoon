# STEM Textbook Translator (Upstage + n8n)

이공계 영어 원서 PDF → 레이아웃을 유지한 한국어 Word/PDF 출력.

## 파이프라인

```
PDF → Upstage Document Parse (객체 단위 분해, 좌표/HTML/base64)
    → Solar LLM 기반 용어집 추출 (2-Pass 1차)
    → 요소별 번역
       · 텍스트 → 자연스러운 한국어
       · 수식(equation) → LaTeX 보존
       · 표(table) → HTML 구조 유지, 셀 텍스트만 번역
       · 그림/차트(figure/chart) → base64 이미지 보존, 캡션 번역
    → python-docx로 재조립
    → (선택) docx2pdf로 PDF 변환
```

n8n은 외부 트리거(웹훅) → FastAPI 호출 → 결과 분기/응답 오케스트레이션을 담당.

## 빠른 시작

```powershell
# 1. 가상환경 & 패키지
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. 환경변수
Copy-Item .env.example .env
notepad .env   # UPSTAGE_API_KEY 입력

# 3. 서버 기동
uvicorn main:app --reload --port 8000
```

테스트:

```powershell
curl.exe -X POST http://localhost:8000/translate `
  -F "file=@samples/textbook.pdf" `
  -F "want_pdf=false"
```

## n8n 연결

1. n8n 실행 (`docker run -p 5678:5678 n8nio/n8n` 또는 데스크탑 앱)
2. Settings → Import from File → `n8n/workflow.json` 선택
3. Webhook 노드의 Test URL을 사용해 PDF 업로드 시 자동 처리
4. FastAPI가 localhost:8000에서 떠있어야 함 (n8n이 도커면 `host.docker.internal`)

## 구조

```
hackerton/
├── main.py                  FastAPI 엔트리포인트 (/translate, /download)
├── pipeline/
│   ├── parser.py            Upstage Document Parse 래퍼
│   ├── solar.py             Solar Chat 클라이언트
│   ├── glossary.py          용어집 추출 (chunked, JSON 강제)
│   ├── translator.py        요소별 번역 + 구조 보존
│   └── docx_builder.py      python-docx 재조립 + 옵션 PDF
├── n8n/workflow.json        n8n 워크플로우 정의
├── requirements.txt
└── .env.example
```

## 알려진 한계 / 다음 단계

- **수식 렌더링**: LaTeX 원문을 Cambria Math로 표기. Word에서 진짜 수식 객체(OMML)로 표시하려면 `latex2mathml` + OMML 변환 단계 추가 필요.
- **2-Pass 용어집 검토 UI 미구현**: 현재는 1차 추출 결과를 그대로 적용. 데모용으로 추출된 용어집을 응답 JSON에 노출하므로 프런트에서 검토 후 재호출하는 흐름으로 확장 가능.
- **PDF 출력**: `docx2pdf`는 MS Word가 설치된 Windows/macOS에서만 동작. 서버 환경엔 LibreOffice headless로 교체 권장.
- **대용량 처리**: 현재는 단일 요청에서 동기 처리. 장시간 PDF는 n8n에서 큐+상태 폴링 패턴으로 분리 권장.
