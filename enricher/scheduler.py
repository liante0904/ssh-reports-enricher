#!/usr/bin/env python3
"""
Enricher Scheduler - 주기적으로 미처리 레포트를 탐색하여 태그 추출을 진행하는 데몬
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

def main():
    interval = int(os.getenv("ENRICHER_INTERVAL_SECONDS", "30"))
    limit = int(os.getenv("ENRICHER_BATCH_LIMIT", "100"))
    
    logger.info(f"[Enricher] Scheduler 데몬 시작 - 실행 주기: {interval}초, 배치 크기: {limit}건")
    
    try:
        enricher = EnricherManager()
    except Exception as e:
        logger.error(f"[Enricher] 데이터베이스 연결 및 초기화 실패: {e}")
        sys.exit(1)
    
    while True:
        try:
            result = enricher.enrich_pending(limit=limit)
            total = result.get("total", 0)
            enriched = result.get("enriched", 0)
            errors = result.get("errors", 0)
            
            if total > 0:
                logger.info(f"[Enricher] 배치 수행 완료: 총 {total}건 중 {enriched}건 태깅 성공 (오류: {errors}건)")
            else:
                # 미처리 건이 없을 때는 디버그 로그만 남겨 조용히 대기
                logger.debug("[Enricher] 미처리 레포트가 존재하지 않습니다.")
                
        except Exception as e:
            logger.error(f"[Enricher] 스케줄러 루프 중 예외 발생: {e}")
            
        time.sleep(interval)

if __name__ == "__main__":
    main()
