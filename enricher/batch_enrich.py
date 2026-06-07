#!/usr/bin/env python3
"""
Batch Enrich - 과거 데이터 일괄 태그 추출

초기 마이그레이션 또는 수동 배치 처리용.
스크래퍼가 insert 후 자동으로 enrich 하므로, 이 스크립트는
초기 28만건 처리나 수동 catch-up 용도로만 사용합니다.

사용법:
  cd apps/scrapers/ssh-reports-scraper
  uv run enricher/batch_enrich.py [limit]

  limit: 한 번에 처리할 레포트 수 (기본값: 100)
"""

import sys
import os

from dotenv import load_dotenv
load_dotenv()

from enricher.enricher_manager import EnricherManager


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    print(f"[Enricher] Batch Enrich 시작 (limit={limit})")

    enricher = EnricherManager()
    result = enricher.enrich_pending(limit=limit)

    print(f"[Enricher] 완료: total={result['total']}, enriched={result['enriched']}, "
          f"skipped={result['skipped']}, errors={result['errors']}")


if __name__ == "__main__":
    main()
