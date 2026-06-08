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
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from enricher.tag_extractor import TagExtractionManager
from enricher.premium_parser import PremiumReportParser

logger = logging.getLogger("enricher")


class EnricherManager:
    """PostgreSQL 연결 + TagExtractionManager를 통합한 enricher 서비스"""

    MAIN_TABLE = "tbl_sec_reports"

    def __init__(self, db_manager=None):
        """
        Args:
            db_manager: 선택적 PostgreSQLManager 인스턴스.
                        제공되면 해당 인스턴스의 연결 설정을 재사용하고,
                        DB 업데이트 시 update_report_tags()를 우선 사용합니다.
                        None이면 .env 기반 자체 연결을 생성합니다.
        """
        self._db = db_manager  # optional PostgreSQLManager from scraper

        if db_manager is not None:
            # scraper의 PostgreSQLManager에서 연결 정보를 읽어옴
            self.host = getattr(db_manager, 'host', 'localhost')
            self.port = getattr(db_manager, 'port', '5432')
            self.database = getattr(db_manager, 'database', 'ssh_reports_hub')
            self.user = getattr(db_manager, 'user', 'ssh_reports_hub')
            self.password = getattr(db_manager, 'password', '')
        else:
            load_dotenv(override=False)
            self.host = os.getenv("POSTGRES_HOST", "localhost")
            self.port = os.getenv("POSTGRES_PORT", "5432")
            self.database = os.getenv("POSTGRES_REPORT_DB", "ssh_reports_hub")
            self.user = os.getenv("POSTGRES_ENRICH_USER", os.getenv("POSTGRES_USER", "ssh_reports_hub"))
            self.password = os.getenv("POSTGRES_PASSWORD", "")

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

    def _get_conn(self):
        return psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.user,
            password=self.password,
        )

    # ── 새 레포트 자동 Enrich (INSERT 후 호출) ──────────────────────

    def enrich_by_keys(self, keys: list[str]) -> dict:
        """
        새로 insert된 레포트들의 key 목록을 받아 태그 추출 + DB 업데이트.
        스크래퍼의 insert_json_data_list 직후 호출됩니다.

        Returns:
            {"enriched": N, "skipped": N, "errors": N}
        """
        if not keys:
            return {"enriched": 0, "skipped": 0, "errors": 0}

        conn = self._get_conn()
        stats = {"enriched": 0, "skipped": 0, "errors": 0}

        try:
            # key 목록으로 report_id + article_title + firm_nm 조회
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

            # 각 레포트에 대해 태그 추출 + 업데이트 (동기식)
            import asyncio
            loop = asyncio.new_event_loop()

            for row in rows:
                try:
                    result = loop.run_until_complete(
                        self.extractor.extract_tags(
                            article_title=row["article_title"],
                            firm_nm=row.get("firm_nm", ""),
                            report_id=row["report_id"],
                        )
                    )

                    if result.get("status") == "success":
                        title = row["article_title"]
                        stock_names = result.get("stock_names", [])

                        opinion = self.premium_parser.parse_target_price_and_rating(title)
                        tickers = self.premium_parser.extract_tickers(title, existing_stocks=stock_names)
                        rep_type = self.premium_parser.classify_report_type(title, tickers)

                        if self._db is not None and hasattr(self._db, 'update_report_tags'):
                            # scraper의 PostgreSQLManager 사용 (이미 async)
                            conn.close()
                            conn = None
                            inner_loop = asyncio.new_event_loop()
                            try:
                                if self.has_premium_columns:
                                    # 프리미엄 파라미터 전달 시도
                                    inner_loop.run_until_complete(
                                        self._db.update_report_tags(
                                            row["report_id"],
                                            result["tags"],
                                            result["stock_names"],
                                            result["sector"],
                                            target_price=opinion["target_price"],
                                            rating=opinion["rating"],
                                            revision_type=opinion["revision_type"],
                                            report_type=rep_type,
                                            stock_tickers=tickers,
                                        )
                                    )
                                else:
                                    inner_loop.run_until_complete(
                                        self._db.update_report_tags(
                                            row["report_id"],
                                            result["tags"],
                                            result["stock_names"],
                                            result["sector"],
                                        )
                                    )
                            except TypeError:
                                # 프리미엄 파라미터를 지원하지 않을 때 (기존 4개 파라미터 전송 후 별도 추가 업데이트)
                                inner_loop.run_until_complete(
                                    self._db.update_report_tags(
                                        row["report_id"],
                                        result["tags"],
                                        result["stock_names"],
                                        result["sector"],
                                    )
                                )
                                if self.has_premium_columns:
                                    conn = self._get_conn()
                                    self._update_premium_only(
                                        conn,
                                        row["report_id"],
                                        target_price=opinion["target_price"],
                                        rating=opinion["rating"],
                                        revision_type=opinion["revision_type"],
                                        report_type=rep_type,
                                        stock_tickers=tickers,
                                    )
                            finally:
                                inner_loop.close()
                                if conn is None:
                                    conn = self._get_conn()
                        else:
                            if self.has_premium_columns:
                                self._update_tags_and_premium(
                                    conn,
                                    row["report_id"],
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
                                    row["report_id"],
                                    result["tags"],
                                    result["stock_names"],
                                    result["sector"],
                                )
                        
                        if conn is not None:
                            conn.commit()
                        stats["enriched"] += 1
                    else:
                        stats["errors"] += 1

                except Exception as e:
                    logger.error(f"Enrich failed for report_id={row['report_id']}: {e}")
                    if conn is not None:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    stats["errors"] += 1

            loop.close()
            if conn is not None:
                try:
                    conn.commit()
                except Exception:
                    pass  # 이미 commit 되었거나 연결이 닫힘

        except Exception as e:
            logger.error(f"enrich_by_keys failed: {e}")
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
        finally:
            if conn is not None:
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
        초기 마이그레이션용 또는 정기 배치 크론용으로 사용.

        Returns:
            {"total": N, "enriched": N, "skipped": N, "errors": N}
        """
        conn = self._get_conn()
        stats = {"total": 0, "enriched": 0, "skipped": 0, "errors": 0}

        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT report_id, firm_nm, article_title
                    FROM {self.MAIN_TABLE}
                    WHERE (tags IS NULL OR tags = '[]'::jsonb OR tags = '[]')
                      AND article_title IS NOT NULL AND article_title != ''
                    ORDER BY report_id DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = [dict(r) for r in cur.fetchall()]

            stats["total"] = len(rows)
            if not rows:
                return stats

            import asyncio
            loop = asyncio.new_event_loop()

            for row in rows:
                try:
                    result = loop.run_until_complete(
                        self.extractor.extract_tags(
                            article_title=row["article_title"],
                            firm_nm=row.get("firm_nm", ""),
                            report_id=row["report_id"],
                        )
                    )
                    if result.get("status") == "success":
                        title = row["article_title"]
                        stock_names = result.get("stock_names", [])

                        opinion = self.premium_parser.parse_target_price_and_rating(title)
                        tickers = self.premium_parser.extract_tickers(title, existing_stocks=stock_names)
                        rep_type = self.premium_parser.classify_report_type(title, tickers)

                        if self._db is not None and hasattr(self._db, 'update_report_tags'):
                            conn.close()
                            conn = None
                            inner_loop = asyncio.new_event_loop()
                            try:
                                if self.has_premium_columns:
                                    inner_loop.run_until_complete(
                                        self._db.update_report_tags(
                                            row["report_id"],
                                            result["tags"],
                                            result["stock_names"],
                                            result["sector"],
                                            target_price=opinion["target_price"],
                                            rating=opinion["rating"],
                                            revision_type=opinion["revision_type"],
                                            report_type=rep_type,
                                            stock_tickers=tickers,
                                        )
                                    )
                                else:
                                    inner_loop.run_until_complete(
                                        self._db.update_report_tags(
                                            row["report_id"],
                                            result["tags"],
                                            result["stock_names"],
                                            result["sector"],
                                        )
                                    )
                            except TypeError:
                                inner_loop.run_until_complete(
                                    self._db.update_report_tags(
                                        row["report_id"],
                                        result["tags"],
                                        result["stock_names"],
                                        result["sector"],
                                    )
                                )
                                if self.has_premium_columns:
                                    conn = self._get_conn()
                                    self._update_premium_only(
                                        conn,
                                        row["report_id"],
                                        target_price=opinion["target_price"],
                                        rating=opinion["rating"],
                                        revision_type=opinion["revision_type"],
                                        report_type=rep_type,
                                        stock_tickers=tickers,
                                    )
                            finally:
                                inner_loop.close()
                                if conn is None:
                                    conn = self._get_conn()
                        else:
                            if self.has_premium_columns:
                                self._update_tags_and_premium(
                                    conn,
                                    row["report_id"],
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
                                    row["report_id"],
                                    result["tags"],
                                    result["stock_names"],
                                    result["sector"],
                                )
                        if conn is not None:
                            conn.commit()
                        stats["enriched"] += 1
                    else:
                        stats["errors"] += 1
                except Exception as e:
                    logger.error(f"Enrich failed for report_id={row['report_id']}: {e}")
                    if conn is not None:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    stats["errors"] += 1

            loop.close()

        except Exception as e:
            logger.error(f"enrich_pending failed: {e}")
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        logger.info(f"[Enricher] enrich_pending: {stats}")
        return stats

    # ── 단일 레포트 Enrich ──────────────────────────────────────────

    async def enrich_one(self, report_id: int) -> dict:
        """단일 레포트에 대해 태그 추출 + DB 업데이트"""
        conn = self._get_conn()
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
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return {"status": "error", "error": str(e)}
        finally:
            if conn is not None:
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
        """태그 정보와 프리미엄 추출 메트릭을 데이터베이스에 일괄 업데이트합니다."""
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
        """기존 3개 인자(tags, stock_names, sector)만 넘기는 오리지널 스키마용 하위 호환 메서드"""
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
        """기존 태그 정보 외에 프리미엄 필드만 선별적으로 업데이트합니다 (Fallback 용)."""
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
