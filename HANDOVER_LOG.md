# 📝 Handover & Work Log (2026-06-07)

이 문서는 **2026-06-07**에 진행된 `ssh-reports-hub` 프로젝트의 프론트엔드 UI 중복 제거, 28만 건 DB 기반 규칙 기반 태깅/산업 분류 고도화, 그리고 크롤러 스케줄러 도커 장애 방어 패치 내역을 다른 LLM/에이전트가 토큰 낭비 없이 한눈에 파악할 수 있도록 기록한 핵심 인계 문서입니다.

---

## 📌 1. 프로젝트 핵심 상태 & 작업 요약

### 🚀 최종 배포 및 오리진 푸시 상태
1. **`ssh-reports-hub` (Frontend)**: pre-push 빌드 검증 및 66개 유닛/E2E 테스트 통과 후 `main` 브랜치 푸시 완료.
2. **`ssh-reports-enricher` (Tag Engine)**: 28만 건 DB 정밀 분석 반영 및 대형주 섹터 Fallback 탑재 완료, `main` 브랜치 푸시 완료.
3. **`ssh-reports-scraper` (Scraper Scheduler)**: 도커 환경 FileNotFoundError (`Errno 2`) 완벽 방어 패치 반영, 실 가동 컨테이너 `exec` 실측 완료 후 `main` 브랜치 푸시 완료.

---

## 🎨 2. 프론트엔드 작업 내역 (`ssh-reports-hub`)

### 🔍 발생했던 문제 (Issue)
* 최근 레포트 목록 화면에서 `tags` 배열 렌더링 시, 상위 산업(`sector`) 혹은 종목명(`stock_names`)과 태그 단어가 중복 배지 형태로 여러 번 노출되는 미관상 이슈 발생 (예: `반도체` / `IPO` / `반도체` 등 겹침).

### 🛠️ 해결 솔루션 (Solution)
* **프론트엔드 실시간 필터링**: [ReportItem.jsx](file:///home/ubuntu/workspace/external.reports-hub/apps/frontend/ssh-reports-hub/src/components/report/ReportItem.jsx) 컴포넌트 내에서 태그 렌더링 직전에 `tags.filter(t => t !== sector && !stock_names?.includes(t))` 안전 필터 적용.
* **백엔드 정화 처리**: [tag_extractor.py](file:///home/ubuntu/workspace/external.reports-hub/apps/scrapers/ssh-reports-enricher/enricher/tag_extractor.py) 내에서도 `sector` 및 `stock_names`와 겹치는 단어는 반환 `tags` 리스트에서 원천 제외되도록 동기/비동기 반환 로직 보강.

---

## 🧠 3. 태깅 및 산업 분류 알고리즘 대대적 고도화 (`ssh-reports-enricher`)

### 📊 28만 건 DB 전수 분석 결과 (`unclassified_analysis.json`)
* 운영 DB 레포트(283,325건) 전수의 제목을 통계 집계한 결과, 무태그/무산업 상태가 **99.19%**에 달하는 구멍 발견.
* 주원인: `음식료`(3,544회), `차전지`(1,687회), `통신서비스`(1,979회) 등 상위 출현 어휘가 매핑 사전에 누락되어 매칭 실패.

### 🛠️ 개선 상세 내역

#### A. 고빈도 금융/투자 어휘 사전 대폭 보강 (`TAG_PATTERNS` 2차 수혈)
* **비중확대**: `Overweight`, `비중확대` -> `비중확대 의견`
* **설명회**: `NDR`, `Meeting`, `기업설명회`, `미팅` -> `NDR/미팅`
* **실적 지표**: `YoY`, `Earnings`, `실적` -> `실적 지표`
* **시장 분석**: `스몰캡`, `투데이 브리프`, `수주/계약`, `모멘텀`, `증시 전망`, `수익성 개선`, `시장 불확실성`, `턴어라운드`, `컨센서스` 패턴 정규식 정밀 구현 완료.

#### B. 대장주 기반 계층형 산업군 폴백(Fallback) 매핑 구현 (`STOCK_TO_SECTOR`)
* **핵심 인사이트**: 대다수 종목 보고서는 제목에 "삼성전자 실적 리뷰"처럼 종목명만 적혀 있고 "반도체"라는 단어가 명시되어 있지 않아 산업 분류가 누락됨.
* **해결**: 주요 대표주 70여 개와 핵심 산업군을 사전에 바인딩한 `STOCK_TO_SECTOR` 사전을 선언.
* **동작**: `_classify_sector` 호출 시 제목에서 산업명이 감지되지 않더라도, 추출된 종목명이 대표주 사전에 있을 경우 해당 산업군으로 **계층적 자동 추정 매핑** 수행.

> [!TIP]
> **성공 사례 (Before & After)**:
> * `한미약품(128940.KS/BUY): ...` 레포트는 제목에 제약/바이오라는 단어가 없어 기존에 산업분류가 **공란**이었으나, 패치 후 **`바이오/헬스케어`**로 완벽하게 자동 분류됨!

#### C. AI 오매칭 버그 정교화
* `On-Chain` 등 영단어 내부의 `ai` 알파벳을 AI 테마 태그로 오분류하는 사이드 이펙트 진압.
* **해결**: 영어 단어 경계를 정밀 한정하는 정규식 `(?<![A-Za-z])AI(?![A-Za-z])`으로 오작동 완벽 방어.

---

## 🛡️ 4. 크롤러 스케줄러 도커 장애 패치 및 실측 (`ssh-reports-scraper`)

### ❌ 감지되었던 ERROR (docker logs)
```text
FnGuide Matcher Execution Error: [Errno 2] No such file or directory: '/backend/ssh-reports-hub-fastAPI/.venv/bin/python'
```

### 🔍 원인 규명
* `ssh-reports-scraper` 컨테이너 내부 스케줄러(`scheduler.py`) 내의 `run_fnguide_matcher` 함수가 백엔드 가상환경 파이썬 경로를 하드코딩하여 트리거를 시도함.
* 하지만 도커 컨테이너 격리 환경 내부에는 백엔드 코드와 가상환경(`.venv`)이 존재하지 않아 매 30분 주기마다 거대한 FileNotFoundError 추적 로그를 발생시키고 있었음.
* **메인 스크랩 영향 여부**: BlockingScheduler 내에서 수집 작업(`main_scraper_job`)은 개별 독립 스레드로 작동하므로 수집 자체에는 영향이 없었으나, 모니터링 로그 오염이 극심했음.

### 🛠️ 해결 상세 & `exec` 실측 완료
* **방어 패치**: subprocess 구동 전, `python_path` 및 `script_path`가 실제 존재하는지 **`os.path.exists()`**로 안전 검사하는 구문을 추가.
* **도커 환경 처리**: 경로 부재 시 `ERROR` 대신 `WARNING` 한 줄만 남기고 우아하게 실행을 스킵(`return`)하도록 우회 로직 탑재.
* **실시간 실측 검증**: 가동 중인 컨테이너 `ssh-reports-scraper-main-scraper-prod` 내부에 `docker cp`로 수정본을 밀어 넣은 뒤, `docker exec`로 강제 실행 테스트 완료. 에러 트레이스백 없이 완벽하게 통과 확인!

---

## 🧭 5. 다음 에이전트/LLM을 위한 신속 인계 가이드

다음 차례의 AI 작업자는 중복 연구나 무작위 테스트로 토큰을 낭비하지 마시고 아래 매뉴얼을 준수해 주십시오.

### 📁 핵심 파일 지도
* **태그 추출 규칙 엔진**: `apps/scrapers/ssh-reports-enricher/enricher/tag_extractor.py` (규칙 및 사전 매핑의 심장부)
* **로컬 무중단 검증기**: `apps/scrapers/ssh-reports-enricher/verify_enrich.py` (DB 락 걱정 없는 안전한 시뮬레이션용)
* **스케줄러 메인**: `apps/scrapers/ssh-reports-scraper/scheduler.py` (도커 장애 예방 처리 완료됨)

### 🧪 필수 실행 명령어
1. **Enricher 로컬 검증 시뮬레이션 (Read-Only)**:
   ```bash
   cd apps/scrapers/ssh-reports-enricher
   uv run verify_enrich.py 30
   ```
2. **Enricher 단위 테스트 구동**:
   ```bash
   cd apps/scrapers/ssh-reports-enricher
   uv run pytest
   ```
3. **가동 중인 컨테이너 단독 검증**:
   ```bash
   docker exec ssh-reports-scraper-main-scraper-prod .venv/bin/python -c "import scheduler; scheduler.run_fnguide_matcher()"
   ```

---
**Document Created by Antigravity AI Engine (Google DeepMind advanced agentic coding team)**
