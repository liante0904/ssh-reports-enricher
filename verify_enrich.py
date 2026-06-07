#!/usr/bin/env python3
"""
Verify Enrich - 로컬 안전 데이터 검증 스크립트

이 스크립트는 oci2_readonly 계정을 사용하여 운영 DB의 데이터를 안전하게 '조회(Read)'한 후,
로컬 메모리상에서 규칙 기반 태그 추출 알고리즘(TagExtractionManager)을 작동시키고
그 결과를 파일로 기록하여 검증하는 용도로 설계되었습니다.

운영 DB에 어떠한 Write 작업(UPDATE, INSERT, DELETE)도 수행하지 않으므로,
데이터가 유실되거나 락(Lock)을 유발할 염려가 전혀 없는 매우 안전한 검증 방식입니다.

사용법:
    cd /home/ubuntu/workspace/external.reports-hub/apps/scrapers/ssh-reports-enricher
    uv run verify_enrich.py [limit]

    limit: 검증용으로 조회할 샘플 레포트 개수 (기본값: 10)
"""

import json
import os
import sys
from loguru import logger
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

# .env 로드
load_dotenv()

# 프로젝트 내부 모듈 경로 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from enricher.tag_extractor import TagExtractionManager

def get_db_connection():
    """env 설정을 활용하여 DB 연결 객체를 반환합니다."""
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = os.getenv("POSTGRES_REPORT_DB", "ssh_reports_hub")
    user = os.getenv("POSTGRES_USER", "oci2_readonly")
    password = os.getenv("POSTGRES_PASSWORD", "")

    logger.info(f"DB 연결 시도 중... (Host: {host}, Port: {port}, Database: {database}, User: {user})")
    
    return psycopg2.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password
    )

def main():
    # 샘플 개수 한도 설정 (기본값: 10건)
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    logger.info(f"로컬 안전 데이터 검증 프로세스를 시작합니다. (대상 샘플 수: {limit}개)")

    # 1. DB 조회
    conn = None
    rows = []
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 락 문제를 유발하지 않도록 단순 SELECT 조회 쿼리 수행
            # 최근 데이터 중 제목이 존재하는 샘플을 LIMIT 단위로 추출
            query = """
                SELECT report_id, firm_nm, article_title, tags, stock_names, sector
                FROM tbl_sec_reports
                WHERE article_title IS NOT NULL AND article_title != ''
                ORDER BY report_id DESC
                LIMIT %s
            """
            cur.execute(query, (limit,))
            rows = [dict(r) for r in cur.fetchall()]
            
        logger.info(f"성공적으로 DB에서 {len(rows)}건의 샘플 레포트 데이터를 조회해 왔습니다.")

    except Exception as e:
        logger.error(f"DB 조회 중 오류가 발생하였습니다: {e}")
        if conn:
            conn.close()
        sys.exit(1)
    finally:
        if conn:
            conn.close()
            logger.info("DB 연결을 안전하게 종료하였습니다. (Read-Only 완료)")

    if not rows:
        logger.warning("조회된 레포트 데이터가 존재하지 않습니다.")
        return

    # 2. 태그 추출 분석 (메모리 내)
    extractor = TagExtractionManager()
    verification_results = []

    logger.info("각 샘플 레포트 제목 분석 및 태그 매칭 시뮬레이션을 진행합니다...")
    for idx, row in enumerate(rows, 1):
        report_id = row["report_id"]
        title = row["article_title"]
        firm = row.get("firm_nm", "")
        
        # 기존 저장된 데이터 확인
        existing_tags = row.get("tags", [])
        existing_stocks = row.get("stock_names", [])
        existing_sector = row.get("sector", "")

        # 신규 규칙 기반 분석 시뮬레이션
        # TagExtractionManager의 동기식 분석 메서드 활용
        analysis_result = extractor.extract_tags_sync(title, firm)
        
        simulated_tags = analysis_result.get("tags", [])
        simulated_stocks = analysis_result.get("stock_names", [])
        simulated_sector = analysis_result.get("sector", "")

        # 검증 구조화
        result_item = {
            "index": idx,
            "report_id": report_id,
            "firm_nm": firm,
            "article_title": title,
            "existing": {
                "tags": existing_tags,
                "stock_names": existing_stocks,
                "sector": existing_sector
            },
            "simulated": {
                "tags": simulated_tags,
                "stock_names": simulated_stocks,
                "sector": simulated_sector
            },
            "matches": {
                "tags_match": (existing_tags == simulated_tags),
                "stocks_match": (existing_stocks == simulated_stocks),
                "sector_match": (existing_sector == simulated_sector)
            }
        }
        verification_results.append(result_item)

        # 터미널 시각적 출력
        print("-" * 80)
        print(f"[{idx}/{limit}] 레포트 ID: {report_id} | 증권사: {firm}")
        print(f"  제목: {title}")
        print(f"  [기존 DB 값] 태그: {existing_tags} | 종목: {existing_stocks} | 산업: {existing_sector}")
        print(f"  [검증 추출] 태그: {simulated_tags} | 종목: {simulated_stocks} | 산업: {simulated_sector}")
        print(f"  일치 여부: 태그({result_item['matches']['tags_match']}) | 종목({result_item['matches']['stocks_match']}) | 산업({result_item['matches']['sector_match']})")

    print("-" * 80)

    # 3. 로컬 파일로 저장
    output_path = "verify_result.json"
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(verification_results, f, ensure_ascii=False, indent=2)
        logger.info(f"✅ 검증 분석 결과가 로컬 파일에 성공적으로 저장되었습니다: {os.path.abspath(output_path)}")
        logger.info("💡 본 검증은 readonly 계정으로 수행되었으며, 실제 DB 데이터는 전혀 변경되지 않았습니다.")
    except Exception as e:
        logger.error(f"결과 파일 저장 중 오류가 발생하였습니다: {e}")

if __name__ == "__main__":
    main()
