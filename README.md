# SSH Reports Enricher — 레포트 데이터 고속 후처리 서비스

본 서비스는 수집된 증권사 레포트 원시 데이터를 정형화하고, 규칙 기반 알고리즘을 통해 **태그(Tags), 관련 종목명(Stock Names), 산업 분류(Sector)**를 정밀하게 고속으로 후처리하는 독자적인 격리 서비스입니다.

기존 스크래퍼 프로젝트(`ssh-reports-scraper`) 내부에서 동기식으로 유발되던 무거운 데이터베이스 락(Lock) 문제를 근본적으로 회피하고, 서비스의 관심사를 격리하기 위해 아키텍처적으로 분리되었습니다.

---

## 1. 아키텍처 개요 (Architecture Overview)

전체 시스템은 **원시 데이터 수집(Write-Heavy)** 영역과 **데이터 가공 및 후처리(Read-Process-Update)** 영역을 물리적/논리적으로 완전히 분리하는 격리 설계를 따릅니다.

```mermaid
flowchart TD
    subgraph Scraper_Project ["1. 스크래퍼 서비스 (Write-Heavy)"]
        Scraper[scraper.py] -->|1. 신규 수집 데이터 삽입| DB[(PostgreSQL: tbl_sec_reports)]
    end

    subgraph Enricher_Project ["2. 엔리처 서비스 (독립 격리)"]
        DB_ReadOnly[("DB (Read-Only)")] -->|2. 미처리 레포트 SELECT| Manager[EnricherManager]
        Manager -->|3. 제목/증권사 데이터 전달| Extractor[TagExtractionManager]
        Extractor -->|4. 고속 규칙 기반 정규식 매칭| Extractor
        Extractor -->|5. 태그/종목/산업 추출| Manager
        
        %% 개발 환경 검증 흐름
        Manager -.->|개발/검증 단계| Verify[verify_enrich.py]
        Verify -.->|6. 안전한 로컬 저장| JSON["verify_result.json (DB 쓰기 없음)"]
        
        %% 운영 반영 흐름
        Manager ==>|운영 단계 (Short Tx)| DB
    end

    classDef prod fill:#2d3748,stroke:#4a5568,color:#fff;
    classDef dev fill:#1a365d,stroke:#2b6cb0,color:#fff;
    class DB,Scraper,Manager,Extractor prod;
    class Verify,JSON dev;
```

---

## 2. 주요 구성 요소 (Key Components)

| 파일/디렉터리 | 역할 및 설명 |
| :--- | :--- |
| `enricher/tag_extractor.py` | **후처리 코어**: LLM(대형 언어 모델)을 사용하지 않고 정규식과 고도화된 사전(Dictionary)만을 사용하는 **규칙 기반 고속 분석 엔진**입니다. 28만 건 이상의 실제 레포트 텍스트 분석에 근거하여 작성되어 초고속/무비용 가공이 가능합니다. |
| `enricher/enricher_manager.py` | **통합 가공 생명주기 관리자**: DB 연결 세션과 추출 코어 간의 생명주기를 중개하며 단일 레포트, 새로 수집된 레포트 키 목록, 또는 과거 펜딩 데이터 처리를 제어합니다. |
| `verify_enrich.py` | **로컬 안전 검증 도구**: 로컬 개발 환경(`oci2`)에서 운영 DB를 망가뜨리지 않고 데이터를 검증하는 안전장치 스크립트입니다. `oci2_readonly` 계정으로 조회를 마친 뒤, 오직 메모리상에서만 분석을 전개하고 결과를 로컬에 파일로 환원합니다. |
| `enricher/backfill_sync.py` | **고속 벌크 백필러**: 아직 가공되지 않은 과거 누적 레포트를 배치(Batch) 단위로 순차 정비하는 스크립트입니다. 매 배치마다 커넥션을 맺고 끊으며 `autocommit=True` 및 `lock_timeout='3s'` 설정을 주입해 DB 락 누적을 방지합니다. |
| `enricher/batch_enrich.py` | **수동 배치 실행 인터페이스**: 과거 유실 데이터를 크론 작업 혹은 수동 명령어로 가볍게 캐치업(Catch-up)할 때 진입점이 되는 일괄 태그 추출 실행기입니다. |

---

## 3. 데이터베이스 락(Lock) 해결을 위한 설계 설계 원칙

본 프로젝트는 과거 데이터베이스 부하 및 락 점유 이슈를 방지하기 위해 다음 원칙을 철저히 준수합니다.

1. **트랜잭션 생명주기 단축 (Short-Lived Transactions)**:
   * 무거운 I/O 바운드 작업(예: 외부 검색, 긴 텍스트 파싱) 도중에는 DB 커넥션을 점유하지 않거나, 단기 커넥션 단위로 세션을 분리 관리합니다.
2. **락 타임아웃 강제 주입 (Lock Timeout Protection)**:
   * 백필 프로그램 실행 시 `SET lock_timeout = '3s'` 쿼리를 선제 인젝션하여, 특정 행의 업데이트 대기가 길어질 경우 커넥션을 대기열에 방치하지 않고 즉시 예외(Fail) 처리 후 다음 루프로 회피합니다.
3. **독립된 롤 기반 접근 제어 (RBAC - Read/Write Separation)**:
   * 로컬 개발 기기(`oci2`)에서는 쓰기 권한이 완전히 배제된 `oci2_readonly` 권한만을 환경에 바인딩하도록 설계되어 운영 데이터를 안전하게 방어합니다.

---

## 4. 로컬 안전 검증 시나리오 구동 가이드

안전 검증 스크립트(`verify_enrich.py`)를 구동하여 운영 DB 변경이나 락 부하 없이 실시간으로 추출 성능 및 가공 정합성을 판정할 수 있습니다.

### 4.1 의존성 설치 및 가상환경 구동
프로젝트 루트에서 `uv` 가상환경 도구를 사용하여 격리된 가속 구동 환경을 생성합니다.
```bash
cd /home/ubuntu/workspace/external.reports-hub/apps/scrapers/ssh-reports-enricher
uv run verify_enrich.py [검증_대상_샘플_수]
```

### 4.2 실행 예시 (5개 샘플 데이터 검증)
```bash
$ uv run verify_enrich.py 5
[INFO] DB 연결 시도 중... (Host: 10.0.0.111, Port: 5432, Database: ssh_reports_hub, User: oci2_readonly)
[INFO] 성공적으로 DB에서 5건의 샘플 레포트 데이터를 조회해 왔습니다. (Read-Only 완료)
--------------------------------------------------------------------------------
[1/5] 레포트 ID: 246719008 | 증권사: 하나증권
  제목: 팸텍 (271830.KQ): IPO 주관사 업데이트: 반도체 주도 성장
  [기존 DB 값] 태그: [] | 종목: [] | 산업: 
  [검증 추출] 태그: ['IPO', '반도체'] | 종목: [] | 산업: 반도체
  일치 여부: 태그(False) | 종목(True) | 산업(False)
--------------------------------------------------------------------------------
...
[INFO] ✅ 검증 분석 결과가 로컬 파일에 성공적으로 저장되었습니다: verify_result.json
[INFO] 💡 본 검증은 readonly 계정으로 수행되었으며, 실제 DB 데이터는 전혀 변경되지 않았습니다.
```

---

## 5. 시크릿 및 환경설정 관리 표준 (Single Source of Truth)

보안과 설정 누수 방지를 위해 원본 환경 변수 파일(`.env`)은 저장소에 커밋하지 않으며, 아래의 자동화된 일방향 파이프라인을 통해서만 배포/적용됩니다.

```
[Git 외부 단일 진실 원천]
~/secrets/workspace/external.reports-hub/apps/scrapers/ssh-reports-enricher/secrets.json
     │
     ▼ (자동 생성기 구동)
python3 ~/secrets/generate_env.py "$PWD"
     │
     ▼ (로컬 마운트)
/home/ubuntu/workspace/external.reports-hub/apps/scrapers/ssh-reports-enricher/.env
```

* `secrets.json`에 정의된 로컬 개발용 DB 환경 변수 세트는 `generate_env.py`에 의해 즉각적인 로컬 로드용 `.env` 구성 파일로 투영됩니다.
