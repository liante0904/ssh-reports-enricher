#!/usr/bin/env python3
"""
고속 동기식 Backfill — tags/sector/stock_names 일괄 채우기

배치마다 독립적인 짧은 커넥션을 사용하여 트랜잭션 락 누적을 방지합니다.
statement_timeout=30s 적용으로 쿼리 블록 시 빠르게 실패합니다.

사용법:
    uv run enricher/backfill_sync.py [batch_size] [max_batches]
"""

import json
import os
import sys
import time

# standalone 실행 시 프로젝트 루트를 path에 추가
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from enricher.tag_extractor import TagExtractionManager

load_dotenv(override=False)

DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": os.getenv("POSTGRES_PORT", "5432"),
    "dbname": os.getenv("POSTGRES_REPORT_DB", "ssh_reports_hub"),
    "user": os.getenv("POSTGRES_ENRICH_USER", os.getenv("POSTGRES_USER", "ssh_reports_hub")),
    "password": os.getenv("POSTGRES_PASSWORD", ""),
}

SELECT_SQL = """
    SELECT report_id, firm_nm, article_title
    FROM tbl_sec_reports
    WHERE (tags IS NULL OR tags = '[]'::jsonb OR tags = '[]')
      AND article_title IS NOT NULL AND article_title != ''
    ORDER BY report_id
    LIMIT %s
"""

UPDATE_SQL = """
    UPDATE tbl_sec_reports
    SET tags = %s, stock_names = %s, sector = %s
    WHERE report_id = %s
"""


def run_backfill(batch_size: int = 5000, max_batches: int = 0):
    extractor = TagExtractionManager()
    batch_num = 0
    total_processed = 0
    total_enriched = 0
    start_time = time.time()

    print(f"[backfill] 배치={batch_size:,} | 최대배치={'무제한' if max_batches==0 else max_batches}")
    print(f"[backfill] DB: {DB_CONFIG['user']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")
    print()

    while True:
        if max_batches > 0 and batch_num >= max_batches:
            break

        batch_num += 1
        batch_start = time.time()
        processed_this_batch = 0

        # ── 매 배치마다 새 커넥션 (락 누적 방지) ──
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = True  # 한 건 한 건 즉시 커밋

        try:
            with conn.cursor() as cur:
                # SELECT에만 30초 타임아웃
                cur.execute("SET statement_timeout = '30s'")

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(SELECT_SQL, (batch_size,))
                rows = [dict(r) for r in cur.fetchall()]

            # SELECT 완료 후 statement_timeout 해제, UPDATE는 lock_timeout만
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = 0")
                cur.execute("SET lock_timeout = '3s'")

            if not rows:
                print(f"[backfill] ✅ 완료 - 더 이상 미처리 레포트 없음")
                conn.close()
                break

            for row in rows:
                result = extractor.extract_tags_sync(
                    row["article_title"],
                    row.get("firm_nm", ""),
                )

                tags_json = json.dumps(result["tags"], ensure_ascii=False)
                stocks_json = json.dumps(result["stock_names"], ensure_ascii=False)
                sector_val = result["sector"] or ""

                try:
                    with conn.cursor() as cur:
                        cur.execute(UPDATE_SQL, (tags_json, stocks_json, sector_val, row["report_id"]))
                    processed_this_batch += 1
                    if result["tags"] or result["stock_names"] or result["sector"]:
                        total_enriched += 1
                except Exception:
                    # 락 타임아웃 등 일시적 실패는 건너뛰고 다음 배치에서 재시도
                    pass

        finally:
            conn.close()

        total_processed += processed_this_batch
        batch_elapsed = time.time() - batch_start
        total_elapsed = time.time() - start_time
        rate = total_processed / total_elapsed if total_elapsed > 0 else 0

        print(
            f"  📦 배치 #{batch_num}: {processed_this_batch:,}건 | "
            f"{batch_elapsed:.1f}초 ({processed_this_batch/batch_elapsed:.0f}건/초) | "
            f"누계: {total_processed:,} ({rate:.0f}건/초)"
        )

    total_elapsed = time.time() - start_time
    print()
    print(f"[backfill] 🏁 완료! 총 {total_processed:,}건 처리, {total_enriched:,}건 enrichment")
    if total_elapsed > 0:
        print(f"[backfill] ⏱️  소요: {total_elapsed:.1f}초 ({total_elapsed/60:.1f}분) | ⚡ {total_processed/total_elapsed:.0f}건/초")
    return total_processed


if __name__ == "__main__":
    batch_size = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    max_batches = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    run_backfill(batch_size, max_batches)
