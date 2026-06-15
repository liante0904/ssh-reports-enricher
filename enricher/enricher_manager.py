"""
Enricher Manager - DB 연동 + 태그 추출 통합

PostgreSQL에 직접 연결하여:
1. 새 레포트 INSERT 직후 자동 태그 추출 (enrich_by_keys)
2. 과거 데이터 배치 처리 (enrich_pending)
3. 단일 레포트 enrichment (enrich_one)

스크래퍼에서 insert_json_data_list 호출 후 이 모듈을 호출하면
크론 없이도 새 데이터에 자동으로 태그가 붙습니다.

이 모듈은 독립적인 DB 연결을 관리하며, scraper의 PostgreSQLManager에
의존하지 않습니다. 추후 별도 서비스로 분리 가능합니다.

## 테이블락 방지 설계 (Table-Lock Prevention)

운영 DB(280K+ rows)에서 배치 작업 중단 원인이었던 테이블락 재발을 방지하기 위해:

1. **모든 연결에 statement_timeout + lock_timeout 적용** (_get_conn)
   - statement_timeout: 장시간 쿼리 블록 → 5s 내 실패
   - lock_timeout: 락 획득 대기 → 3~5s 내 포기
   - 연결 옵션이 아닌 SET 명령 사용 → PgBouncer 호환

2. **enrich_pending() → 부분 인덱스(idx_sec_reports_tags_null) 활용**
   - WHERE (tags IS NULL OR tags = '[]'::jsonb) → index scan
   - jsonb_array_length() 함수 제거 → 인덱스 사용 불가 함수
   - ORDER BY report_id DESC → 부분 인덱스 정렬 활용

3. **match_fnguide_summaries() / backfill_fnguide_pdf_urls()**
   - lock_timeout=3s 명시적 SET
   - fnguide_summary_id/pf_url NULL 체크에 부분 인덱스 활용

4. **모든 UPDATE → PK(report_id) 기반 단일 행 갱신**
   - WHERE report_id = %s → 항상 PK index scan
   - 전체 테이블 스캔 UPDATE 없음

5. **scheduler.py → idle backoff**
   - 미처리 레포트 없으면 점진적 대기 시간 증가 (30s → 300s)
   - 불필요한 seq scan 방지

필수 선행 인덱스: sql/batch_operation_indexes.sql 실행 필요
"""


import json
import logging
import os
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras

try:
    from ssh_library.database import BasePostgreSQLManager
except ImportError:
    from dotenv import load_dotenv
    load_dotenv()
    
    class BasePostgreSQLManager:
        """ssh_library 미설치 시 fallback"""
        def __init__(self):
            self.host = os.getenv("POSTGRES_HOST", "localhost")
            self.port = os.getenv("POSTGRES_PORT", "5432")
            self.database = os.getenv("POSTGRES_REPORT_DB", "ssh_reports_hub")
            self.user = os.getenv("POSTGRES_ENRICH_USER", os.getenv("POSTGRES_USER", "ssh_reports_hub"))
            self.password = os.getenv("POSTGRES_PASSWORD", "")

from enricher.tag_extractor import TagExtractionManager
from enricher.premium_parser import PremiumReportParser

logger = logging.getLogger("enricher")


class EnricherManager(BasePostgreSQLManager):
    """PostgreSQL 연결 + TagExtractionManager를 통합한 enricher 서비스"""

    MAIN_TABLE = "tbl_sec_reports"

    def __init__(self, db_manager=None):
        """
        Args:
            db_manager: 선택적 PostgreSQLManager 인스턴스.
                        제공되면 해당 인스턴스의 연결 설정을 재사용하고,
                        DB 업데이트 시 update_report_tags()를 우선 사용합니다.
        """
        super().__init__()
        self._db = db_manager

        if db_manager is not None:
            self.host = getattr(db_manager, 'host', self.host)
            self.port = getattr(db_manager, 'port', self.port)
            self.database = getattr(db_manager, 'database', self.database)
            self.password = getattr(db_manager, 'password', '')

        self.extractor = TagExtractionManager()
        self.premium_parser = PremiumReportParser()
        self.has_premium_columns = self._check_premium_columns()
        logger.info(f"[EnricherManager] 프리미엄 필드 동적 활성화 상태: {self.has_premium_columns}")

    def _check_premium_columns(self) -> bool:
        """데이터베이스 테이블에 프리미엄 필드 컬럼(target_price 등)이 존재하는지 동적으로 확인합니다."""
        conn = None
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = %s 
                      AND column_name = 'target_price'
                    """,
                    (self.MAIN_TABLE,),
                )
                return cur.fetchone() is not None
        except Exception as e:
            logger.warning(f"[EnricherManager] 프리미엄 컬럼 동적 체크 실패 (Fallback 3필드 활성화): {e}")
            return False
        finally:
            if conn is not None:
                conn.close()

    # ── Connection ──────────────────────────────────────────────────

    def _get_conn(self, statement_timeout: str = None, lock_timeout: str = None):
        """PostgreSQL 연결 생성 (session-level timeout 설정).

        연결 직후 SET 명령으로 statement_timeout + lock_timeout을 세션에 적용.
        PgBouncer/커넥션 풀러와 호환되며, backfill_sync.py와 동일한 패턴.

        Args:
            statement_timeout: 쿼리 타임아웃 (기본 '30s')
            lock_timeout: 락 획득 타임아웃 (기본 '5s')
        """
        conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=self.password,
            connect_timeout=10,
        )
        # session-level timeout 설정 (connection options보다 portable)
        stmt_to = statement_timeout or '30s'
        lock_to = lock_timeout or '5s'
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = '{stmt_to}'")
            cur.execute(f"SET lock_timeout = '{lock_to}'")
        return conn

    # ── 단일 행 Enrich (공유 로직) ──────────────────────────────────

    def _enrich_row_sync(self, row: dict, conn) -> bool:
        """단일 레포트 행에 대해 태그 추출 → 프리미엄 파싱 → DB 업데이트.

        enrich_by_keys()와 enrich_pending()에서 공유하는 핵심 파이프라인.
        LLM 혼동 방지를 위해 단일 메서드로 통합.

        Args:
            row: {"report_id", "firm_nm", "article_title"} dict
            conn: psycopg2 connection (이미 timeout 설정된 세션)

        Returns:
            True if enriched successfully, False otherwise.
        """
        import asyncio

        # 1. 태그 추출 (async → sync 브릿지)
        result = asyncio.run(
            self.extractor.extract_tags(
                article_title=row["article_title"],
                firm_nm=row.get("firm_nm", ""),
                report_id=row["report_id"],
            )
        )
        if result.get("status") != "success":
            return False

        title = row["article_title"]
        stock_names = result.get("stock_names", [])

        # 2. 프리미엄 파싱 (목표가, 투자의견, 종목코드, 리포트 유형)
        opinion = self.premium_parser.parse_target_price_and_rating(title)
        tickers = self.premium_parser.extract_tickers(title, existing_stocks=stock_names)
        rep_type = self.premium_parser.classify_report_type(title, tickers)

        # 3. DB 업데이트 — 우선순위: _db.update_report_tags > 직접 UPDATE
        if self._db is not None and hasattr(self._db, 'update_report_tags'):
            self._update_via_scraper_db(row["report_id"], result, opinion, tickers, rep_type, conn)
        else:
            self._update_via_direct_conn(conn, row["report_id"], result, opinion, tickers, rep_type)

        return True

    def _update_via_scraper_db(self, report_id, tag_result, opinion, tickers, rep_type, conn):
        """scraper의 PostgreSQLManager.update_report_tags()를 통한 업데이트.

        scraper와 enricher가 동일 프로세스에서 실행될 때 사용.
        update_report_tags는 async 메서드이므로 asyncio.run()으로 브릿지.
        """
        import asyncio

        try:
            if self.has_premium_columns:
                asyncio.run(
                    self._db.update_report_tags(
                        report_id,
                        tag_result["tags"],
                        tag_result["stock_names"],
                        tag_result["sector"],
                        target_price=opinion["target_price"],
                        rating=opinion["rating"],
                        revision_type=opinion["revision_type"],
                        report_type=rep_type,
                        stock_tickers=tickers,
                    )
                )
            else:
                asyncio.run(
                    self._db.update_report_tags(
                        report_id,
                        tag_result["tags"],
                        tag_result["stock_names"],
                        tag_result["sector"],
                    )
                )
        except TypeError:
            # 구형 update_report_tags (프리미엄 파라미터 미지원)
            asyncio.run(
                self._db.update_report_tags(
                    report_id,
                    tag_result["tags"],
                    tag_result["stock_names"],
                    tag_result["sector"],
                )
            )
            if self.has_premium_columns:
                self._update_premium_only(
                    conn, report_id,
                    target_price=opinion["target_price"],
                    rating=opinion["rating"],
                    revision_type=opinion["revision_type"],
                    report_type=rep_type,
                    stock_tickers=tickers,
                )

    def _update_via_direct_conn(self, conn, report_id, tag_result, opinion, tickers, rep_type):
        """enricher 자체 DB 연결을 통한 직접 UPDATE.

        scraper와 분리된 standalone 모드에서 사용.
        PK(report_id) 기반 단일 행 UPDATE — 항상 인덱스 스캔.
        """
        if self.has_premium_columns:
            self._update_tags_and_premium(
                conn, report_id,
                tag_result["tags"],
                tag_result["stock_names"],
                tag_result["sector"],
                target_price=opinion["target_price"],
                rating=opinion["rating"],
                revision_type=opinion["revision_type"],
                report_type=rep_type,
                stock_tickers=tickers,
            )
        else:
            self._update_tags(
                conn, report_id,
                tag_result["tags"],
                tag_result["stock_names"],
                tag_result["sector"],
            )

    # ── 새 레포트 자동 Enrich (INSERT 후 호출) ──────────────────────

    def enrich_by_keys(self, keys: list[str]) -> dict:
        """
        새로 insert된 레포트들의 key 목록을 받아 태그 추출 + DB 업데이트.
        스크래퍼의 insert_json_data_list 직후 호출됩니다.

        파이프라인: SELECT by keys → _enrich_row_sync() → commit
        key = ANY(%s) → unique index scan (테이블락 없음)

        Returns:
            {"enriched": N, "skipped": N, "errors": N}
        """
        if not keys:
            return {"enriched": 0, "skipped": 0, "errors": 0}

        conn = self._get_conn(statement_timeout="10s")
        stats = {"enriched": 0, "skipped": 0, "errors": 0}

        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT report_id, firm_nm, article_title
                    FROM {self.MAIN_TABLE}
                    WHERE key = ANY(%s)
                      AND article_title IS NOT NULL
                      AND article_title != ''
                    """,
                    (keys,),
                )
                rows = [dict(r) for r in cur.fetchall()]

            if not rows:
                return stats

            for row in rows:
                try:
                    if self._enrich_row_sync(row, conn):
                        stats["enriched"] += 1
                    else:
                        stats["errors"] += 1
                except Exception as e:
                    logger.error(f"Enrich failed for report_id={row['report_id']}: {e}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    stats["errors"] += 1

            conn.commit()

        except Exception as e:
            logger.error(f"enrich_by_keys failed: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if stats["enriched"] > 0:
            logger.info(f"[Enricher] enrich_by_keys: {stats}")
        return stats

    # ── 과거 데이터 배치 처리 ────────────────────────────────────────

    def enrich_pending(self, limit: int = 50) -> dict:
        """
        태그가 없는 과거 레포트를 배치 처리합니다.

        테이블락 방지 설계:
        - 부분 인덱스(idx_sec_reports_tags_null)로 seq scan → index scan
        - jsonb_array_length() 제거 (인덱스 사용 불가 함수)
        - statement_timeout=10s + lock_timeout=5s
        - 한 건씩 commit → 락 점유 시간 최소화

        Returns:
            {"total": N, "enriched": N, "skipped": N, "errors": N}
        """
        conn = self._get_conn(statement_timeout="10s")
        stats = {"total": 0, "enriched": 0, "skipped": 0, "errors": 0}

        try:
            # 부분 인덱스 활용: tags IS NULL OR tags = '[]'::jsonb
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT report_id, firm_nm, article_title
                    FROM {self.MAIN_TABLE}
                    WHERE (tags IS NULL OR tags = '[]'::jsonb OR tags = '[]')
                      AND article_title IS NOT NULL
                      AND article_title != ''
                    ORDER BY report_id DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = [dict(r) for r in cur.fetchall()]

            stats["total"] = len(rows)
            if not rows:
                return stats

            for row in rows:
                try:
                    if self._enrich_row_sync(row, conn):
                        conn.commit()
                        stats["enriched"] += 1
                    else:
                        stats["errors"] += 1
                except Exception as e:
                    logger.error(f"Enrich failed for report_id={row['report_id']}: {e}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    stats["errors"] += 1

        except Exception as e:
            logger.error(f"enrich_pending failed: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if stats["total"] > 0:
            logger.info(f"[Enricher] enrich_pending: {stats}")
        return stats

    # ── 단일 레포트 Enrich ──────────────────────────────────────────

    async def enrich_one(self, report_id: int) -> dict:
        """단일 레포트에 대해 태그 추출 + DB 업데이트 (API용)"""
        conn = self._get_conn(statement_timeout="10s")
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"SELECT report_id, firm_nm, article_title FROM {self.MAIN_TABLE} WHERE report_id = %s",
                    (report_id,),
                )
                row = cur.fetchone()
                if not row:
                    return {"status": "error", "error": "report not found"}

            result = await self.extractor.extract_tags(
                article_title=row["article_title"],
                firm_nm=row.get("firm_nm", ""),
                report_id=report_id,
            )
            if result.get("status") == "success":
                title = row["article_title"]
                stock_names = result.get("stock_names", [])

                opinion = self.premium_parser.parse_target_price_and_rating(title)
                tickers = self.premium_parser.extract_tickers(title, existing_stocks=stock_names)
                rep_type = self.premium_parser.classify_report_type(title, tickers)

                # 단일 조회 API 응답 데이터도 프리미엄 사양으로 격상
                result["target_price"] = opinion["target_price"]
                result["rating"] = opinion["rating"]
                result["revision_type"] = opinion["revision_type"]
                result["report_type"] = rep_type
                result["stock_tickers"] = tickers

                if self._db is not None and hasattr(self._db, 'update_report_tags'):
                    try:
                        if self.has_premium_columns:
                            await self._db.update_report_tags(
                                report_id,
                                result["tags"],
                                result["stock_names"],
                                result["sector"],
                                target_price=opinion["target_price"],
                                rating=opinion["rating"],
                                revision_type=opinion["revision_type"],
                                report_type=rep_type,
                                stock_tickers=tickers,
                            )
                        else:
                            await self._db.update_report_tags(
                                report_id,
                                result["tags"],
                                result["stock_names"],
                                result["sector"],
                            )
                    except TypeError:
                        await self._db.update_report_tags(
                            report_id,
                            result["tags"],
                            result["stock_names"],
                            result["sector"],
                        )
                        if self.has_premium_columns:
                            self._update_premium_only(
                                conn,
                                report_id,
                                target_price=opinion["target_price"],
                                rating=opinion["rating"],
                                revision_type=opinion["revision_type"],
                                report_type=rep_type,
                                stock_tickers=tickers,
                            )
                else:
                    if self.has_premium_columns:
                        self._update_tags_and_premium(
                            conn,
                            report_id,
                            result["tags"],
                            result["stock_names"],
                            result["sector"],
                            target_price=opinion["target_price"],
                            rating=opinion["rating"],
                            revision_type=opinion["revision_type"],
                            report_type=rep_type,
                            stock_tickers=tickers,
                        )
                    else:
                        self._update_tags(
                            conn,
                            report_id,
                            result["tags"],
                            result["stock_names"],
                            result["sector"],
                        )
                    conn.commit()
            return result
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return {"status": "error", "error": str(e)}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ── 내부 유틸리티 ────────────────────────────────────────────────

    def _update_tags_and_premium(
        self,
        conn,
        report_id: int,
        tags: list,
        stock_names: list,
        sector: str,
        target_price: Optional[int] = None,
        rating: Optional[str] = None,
        revision_type: Optional[str] = None,
        report_type: Optional[str] = None,
        stock_tickers: Optional[list] = None,
    ):
        """태그 정보와 프리미엄 추출 메트릭을 데이터베이스에 일괄 업데이트합니다.

        PK(report_id) 기반 단일 행 UPDATE — 항상 인덱스 스캔, 락 범위 최소.
        """
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {self.MAIN_TABLE}
                SET tags = %s,
                    stock_names = %s,
                    sector = %s,
                    target_price = %s,
                    rating = %s,
                    revision_type = %s,
                    report_type = %s,
                    stock_tickers = %s
                WHERE report_id = %s
                """,
                (
                    json.dumps(tags, ensure_ascii=False),
                    json.dumps(stock_names, ensure_ascii=False),
                    sector or "",
                    target_price,
                    rating,
                    revision_type,
                    report_type,
                    json.dumps(stock_tickers or [], ensure_ascii=False),
                    report_id,
                ),
            )

    def _update_tags(self, conn, report_id: int, tags: list, stock_names: list, sector: str):
        """기존 3개 인자(tags, stock_names, sector)만 넘기는 오리지널 스키마용 하위 호환 메서드

        PK(report_id) 기반 단일 행 UPDATE — 항상 인덱스 스캔.
        """
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {self.MAIN_TABLE}
                SET tags = %s,
                    stock_names = %s,
                    sector = %s
                WHERE report_id = %s
                """,
                (
                    json.dumps(tags, ensure_ascii=False),
                    json.dumps(stock_names, ensure_ascii=False),
                    sector or "",
                    report_id,
                ),
            )

    def _update_premium_only(
        self,
        conn,
        report_id: int,
        target_price: Optional[int] = None,
        rating: Optional[str] = None,
        revision_type: Optional[str] = None,
        report_type: Optional[str] = None,
        stock_tickers: Optional[list] = None,
    ):
        """기존 태그 정보 외에 프리미엄 필드만 선별적으로 업데이트합니다 (Fallback 용).

        PK(report_id) 기반 단일 행 UPDATE — 항상 인덱스 스캔.
        """
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {self.MAIN_TABLE}
                SET target_price = %s,
                    rating = %s,
                    revision_type = %s,
                    report_type = %s,
                    stock_tickers = %s
                WHERE report_id = %s
                """,
                (
                    target_price,
                    rating,
                    revision_type,
                    report_type,
                    json.dumps(stock_tickers or [], ensure_ascii=False),
                    report_id,
                ),
            )

    # ── FnGuide Summary 매칭 ──────────────────────────────────────

    def match_fnguide_summaries(self, batch_size: int = 200) -> dict:
        """tbl_sec_reports ↔ tbl_fnguide_report_summaries 매칭.

        증권사명 + 날짜 + 기업명으로 매칭하여 fnguide_summary_id를 채웁니다.

        - lock_timeout=3s 적용 → 락 획득 실패시 즉시 포기
        - statement_timeout=30s → 장시간 쿼리 블록 방지
        - fnguide_summary_id IS NULL 조건에 부분 인덱스 활용
        """
        conn = self._get_conn(statement_timeout="30s")
        stats = {"matched": 0, "errors": 0}
        try:
            with conn.cursor() as cur:
                # lock_timeout 즉시 적용 (매칭 UPDATE는 락 경합 가능성 높음)
                cur.execute("SET lock_timeout = '3s'")
                cur.execute(f"""
                    UPDATE {self.MAIN_TABLE} r
                    SET fnguide_summary_id = s.summary_id
                    FROM tbl_fnguide_report_summaries s
                    WHERE r.firm_nm = s.provider
                      AND r.fnguide_summary_id IS NULL
                      AND s.author LIKE r.writer || '%%'
                      AND r.reg_dt::integer - REPLACE(s.report_date, '.', '')::integer BETWEEN 0 AND 3
                      AND r.article_title LIKE '%%' || s.company_name || '%%'
                      AND s.summary_id IN (
                          SELECT summary_id FROM tbl_fnguide_report_summaries
                          ORDER BY summary_id LIMIT {batch_size}
                      )
                """)
                stats["matched"] = cur.rowcount
                conn.commit()
        except Exception as e:
            logger.error(f"match_fnguide_summaries failed: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            stats["errors"] += 1
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if stats["matched"] >= 0:
            logger.info(f"[Enricher] FnGuide 매칭: {stats['matched']}건")
        return stats

    
    # article_url은 FnGuide 원장 URL이므로 새 컬럼 또는 JOIN으로 구현 필요

    def backfill_fnguide_pdf_urls(self, batch_size: int = 500) -> dict:
        """역매칭: sec_reports.telegram_url → fnguide_report_summaries.pdf_url

        - lock_timeout=3s 적용
        - statement_timeout=30s 적용
        - pdf_url NULL 부분 인덱스 활용
        """
        conn = self._get_conn(statement_timeout="30s")
        stats = {"updated": 0, "errors": 0}
        try:
            with conn.cursor() as cur:
                cur.execute("SET lock_timeout = '3s'")
                cur.execute(f"""
                    UPDATE tbl_fnguide_report_summaries s
                    SET pdf_url = r.telegram_url
                    FROM {self.MAIN_TABLE} r
                    WHERE s.summary_id = r.fnguide_summary_id
                      AND r.telegram_url IS NOT NULL AND r.telegram_url != ''
                      AND (s.pdf_url IS NULL OR s.pdf_url = '')
                      AND s.summary_id IN (
                          SELECT summary_id FROM tbl_fnguide_report_summaries
                          WHERE pdf_url IS NULL OR pdf_url = ''
                          ORDER BY summary_id LIMIT {batch_size}
                      )
                """)
                stats["updated"] = cur.rowcount
                conn.commit()
        except Exception as e:
            logger.error(f"backfill_fnguide_pdf_urls: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            stats["errors"] += 1
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if stats["updated"] >= 0:
            logger.info(f"[Enricher] PDF URL 역매칭: {stats['updated']}건")
        return stats
