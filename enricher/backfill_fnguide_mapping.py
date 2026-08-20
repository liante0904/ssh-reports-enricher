"""전체 기간 FnGuide summary 매핑 백필 실행기.

하루 단위로만 UPDATE를 실행해 장시간 트랜잭션과 테이블 락을 피합니다.
기본 범위는 tbl_sec_reports의 가장 오래된 report_date부터 오늘까지이며,
offset 인자로 중단 후 특정 구간을 재실행할 수 있습니다.
"""

import argparse
import logging
import time

from enricher.enricher_manager import EnricherManager


def _date_bounds(enricher: EnricherManager) -> tuple[int, int]:
    conn = enricher._get_conn(statement_timeout="10s", lock_timeout="3s")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT CURRENT_DATE - MIN(report_date::date) "
                "FROM tbl_sec_reports WHERE report_date IS NOT NULL"
            )
            max_offset = int(cur.fetchone()[0] or 0)
        return max_offset, 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run full-history FnGuide mapping backfill")
    parser.add_argument("--start-offset", type=int, default=None, help="시작 D-n (기본: 가장 오래된 날짜)")
    parser.add_argument("--end-offset", type=int, default=0, help="종료 D-n (기본: 오늘)")
    parser.add_argument("--sleep", type=float, default=0.2, help="날짜별 UPDATE 사이 대기 초")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    enricher = EnricherManager()
    oldest_offset, _ = _date_bounds(enricher)
    start = oldest_offset if args.start_offset is None else args.start_offset
    end = args.end_offset
    if start < end or end < 0:
        parser.error("start-offset은 end-offset 이상이어야 합니다")

    logging.info("FnGuide 전체 매핑 백필 시작: D-%d ~ D-%d", start, end)
    total_matched = 0
    total_errors = 0
    for offset in range(start, end - 1, -1):
        result = enricher.match_fnguide_summaries(date_offset_days=offset)
        matched = result.get("matched", 0)
        errors = result.get("errors", 0)
        total_matched += matched
        total_errors += errors
        logging.info("D-%d 완료: matched=%d errors=%d 누계=%d", offset, matched, errors, total_matched)
        if args.sleep > 0:
            time.sleep(args.sleep)

    logging.info("FnGuide 전체 매핑 백필 완료: matched=%d errors=%d", total_matched, total_errors)
    return 0 if total_errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
