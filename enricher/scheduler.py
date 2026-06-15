#!/usr/bin/env python3
"""
Enricher Scheduler - 주기적으로 미처리 레포트를 탐색하여 태그 추출을 진행하는 데몬

테이블락 방지 설계:
- enrich_pending: 30초~300초 간격, 미처리 건 없으면 점진적으로 대기 시간 증가 (idle backoff)
- FnGuide 매칭: 5분 간격, lock_timeout=3s 즉시 적용
- 모든 쿼리에 statement_timeout + lock_timeout 적용 (enricher_manager.py)
"""

import os
import sys
import time
from loguru import logger
from dotenv import load_dotenv

# .env 로드
load_dotenv()

# standalone 실행 시 프로젝트 루트를 path에 추가
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from enricher.enricher_manager import EnricherManager

# ── idle backoff 설정 ──
IDLE_BACKOFF_MIN = int(os.getenv("ENRICHER_IDLE_BACKOFF_MIN", "30"))      # 최소 대기 (초)
IDLE_BACKOFF_MAX = int(os.getenv("ENRICHER_IDLE_BACKOFF_MAX", "300"))     # 최대 대기 (초, 5분)
IDLE_BACKOFF_FACTOR = float(os.getenv("ENRICHER_IDLE_BACKOFF_FACTOR", "2.0"))  # 배수 증가
IDLE_BACKOFF_DECREASE = int(os.getenv("ENRICHER_IDLE_BACKOFF_DECREASE", "1"))   # 작업 발견 시 감소 단계


def main():
    interval = int(os.getenv("ENRICHER_INTERVAL_SECONDS", "30"))
    limit = int(os.getenv("ENRICHER_BATCH_LIMIT", "100"))
    fnguide_interval_ticks = int(os.getenv("ENRICHER_FNGUIDE_INTERVAL_TICKS", "10"))  # 10회마다 FnGuide

    # idle backoff 상태
    idle_ticks = 0       # 연속 idle tick 카운트
    current_backoff = IDLE_BACKOFF_MIN

    logger.info(
        f"[Enricher] Scheduler 데몬 시작 - 실행 주기: {interval}초, 배치 크기: {limit}건, "
        f"idle backoff: {IDLE_BACKOFF_MIN}s~{IDLE_BACKOFF_MAX}s"
    )

    try:
        enricher = EnricherManager()
    except Exception as e:
        logger.error(f"[Enricher] 데이터베이스 연결 및 초기화 실패: {e}")
        sys.exit(1)

    tick = 0

    while True:
        tick += 1
        try:
            result = enricher.enrich_pending(limit=limit)
            total = result.get("total", 0)
            enriched = result.get("enriched", 0)
            errors = result.get("errors", 0)

            if total > 0:
                logger.info(
                    f"[Enricher] 배치 #{tick}: 총 {total}건 중 {enriched}건 태깅 성공 "
                    f"(오류: {errors}건) | backoff 리셋"
                )
                # 작업 발견 → idle backoff 리셋
                idle_ticks = 0
                current_backoff = IDLE_BACKOFF_MIN
            else:
                idle_ticks += 1
                # 점진적 backoff 증가
                current_backoff = min(
                    IDLE_BACKOFF_MAX,
                    IDLE_BACKOFF_MIN * (IDLE_BACKOFF_FACTOR ** min(idle_ticks - 1, 10))
                )
                current_backoff = int(current_backoff)
                logger.debug(
                    f"[Enricher] idle #{idle_ticks} → 다음 backoff: {current_backoff}s"
                )

        except Exception as e:
            logger.error(f"[Enricher] 스케줄러 루프 중 예외 발생: {e}")

        # FnGuide 매칭: 5분마다 (interval 30초 × 10회)
        # 단, idle backoff 시에는 현재 대기 시간에 맞춰 조정
        if tick % fnguide_interval_ticks == 0:
            try:
                fnguide_result = enricher.match_fnguide_summaries(batch_size=200)
                pdf_result = enricher.backfill_fnguide_pdf_urls(batch_size=500)
            except Exception as e:
                logger.error(f"[Enricher] FnGuide 매칭 중 예외: {e}")

        # idle backoff에 따라 대기 시간 조정
        sleep_time = max(interval, current_backoff) if idle_ticks > 0 else interval
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
