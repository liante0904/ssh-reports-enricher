# External Reports Enricher (AI Summary & Tagging Engine) Flow

이 문서는 **External Reports Hub의 AI 데이터 고도화 엔진(`ssh-reports-enricher`)**의 데이터 분석, PDF 텍스트 추출, 그리고 LLM(Gemini / DeepSeek) 연동 분석 처리 흐름을 설명하여 다른 LLM이 AI 가공 원리를 소스코드를 보지 않고도 빠르게 이해하도록 돕습니다.

---

## 1. AI 인리처 처리 파이프라인 흐름도 (AI Enricher Pipeline Flow)

수집기에서 1차 적재된 로 데이터(`tbl_sec_reports`) 중 아직 요약이나 종목 분석이 되지 않은 건(`sync_status = 0`)들을 순차적으로 읽어와 분석 고도화를 수행하는 파이프라인 구조입니다.

```mermaid
flowchart TD
    %% 1. 대기 건 탐색
    Trigger["Enricher Scheduler Trigger"] --> ScanDB["Scan DB (tbl_sec_reports)\nWHERE sync_status = 0 (대기)"]
    
    %% 2. PDF 다운로드 및 아카이빙 확인
    ScanDB --> MatchTarget{"Check PDF Archive Status\n(다운로드 및 저장 여부 검사)"}
    MatchTarget -->|No / INIT| RequestArchive["Trigger PDF Archiver Service\n(PDF 아카이빙 수동 요청 및 완료 대기)"]
    MatchTarget -->|Yes / SUCCESS| ReadPDFText["Extract PDF Content\n(PDF에서 원시 텍스트 및 메타 추출)"]
    
    RequestArchive --> ReadPDFText

    %% 3. AI 분석 레이어
    subgraph "AI Core Processing Engine"
        ReadPDFText --> CleanText["Clean raw text\n(불필요한 공백/개행 제거 및 문자열 정제)"]
        CleanText --> ModelSelect{"Select LLM Model\n(Gemini 1.5 Pro / DeepSeek)"}
        
        ModelSelect -->|Prompt 1| AISummary["AI Three-Line Summary\n(리포트 세 줄 요약 생성)"]
        ModelSelect -->|Prompt 2| AISentiment["AI Sentiment Analysis\n(긍정/부정/중립 판별 & 중요도 점수)"]
        ModelSelect -->|Prompt 3| AITagging["AI Stock & Sector Extraction\n(관련 기업명 및 섹터 태그 추출)"]
    end

    %% 4. DB 갱신 및 완료 처리
    AISummary & AISentiment & AITagging --> AssembleJSON["Assemble Result Payload\n(JSON 포맷으로 분석 결과 통합)"]
    AssembleJSON --> UpdateDB["Update DB (tbl_sec_reports)\n- gemini_summary = 요약문\n- tags = 태그 JSON\n- stock_names = 관련 기업 목록\n- sync_status = 2 (처리 완료)"]
    
    %% 예외 처리
    CleanText -->|Text extraction failed / Empty| HandleError["Update sync_status = -1 (장애)\nRecord Error Log"]
end
```

---

## 2. 주요 연계 API 및 분석 프로프트 항목 (AI Prompts & Parameters)

AI 인리칭의 성공적인 작동을 위해 LLM에 전달되는 정밀한 프롬프트 템플릿과 설정 변수 정보입니다.

### 2.1 주요 가공 파라미터 및 프롬프트 핵심 규칙
* **세 줄 요약 규칙**: 
  1. 리포트 본문의 목적, 핵심 투자 근거, 그리고 투자의견/목표주가 변화 여부를 명확히 한 문장씩 총 세 문장으로 요약할 것.
  2. 전문 용어나 어려운 한자는 되도록 쉬운 한글 금융 용어로 번역하여 제공할 것.
* **종목/섹터 추출 규칙**:
  - 리포트가 집중 타깃하고 있는 종목명을 배열 형태로 추출(예: `["삼성전자", "SK하이닉스"]`).
  - 매칭되는 정확한 코스피/코스닥 표준 업종 분류명(예: `반도체`, `전기전자`)을 매핑할 것.
* **감성 평가 지표**:
  - `sentiment`: 긍정(Positive), 중립(Neutral), 부정(Negative) 3단계 구분.
  - `importance`: 0.0(일반 공시 수준) ~ 1.0(연간 실적 가이드라인 변경 등 핵심 뉴스) 사이의 실수 점수 산출.

---

## 3. 에러 감지 및 자동 복구 메커니즘 (Self-Healing & Error Handling)

* **상태값 -1(실패) 복구**: 가끔씩 LLM API의 일시적 레이트 리밋(Rate Limit)이나 네트워크 타임아웃 등으로 인해 요약이 실패하면 `sync_status = -1`로 적재됩니다.
* **자동 재시도(Retry Count)**: 인리처 루프는 `retry_count`가 3회 미만인 실패 건들을 매번 선별하여 일정 시간 간격으로 자동 재시도를 처리해 줍니다. 최종 3회 이상 실패 시 관리자 알림 및 모니터링 허브에 장애 상태로 등록됩니다.
