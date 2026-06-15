"""
Table-Lock Prevention Tests — 배치 운영 중단 원인이었던 테이블락 재발 방지

검증 대상:
1. enrich_pending SELECT → jsonb_array_length() 미사용 (인덱스 불가 함수)
2. enrich_pending SELECT → 부분 인덱스 활용 가능한 WHERE 절
3. _get_conn() → SET statement_timeout + lock_timeout 적용
4. match_fnguide_summaries() → SET lock_timeout 적용
5. UPDATE문 → PK 기반 단일 행 갱신 (index scan 보장)
6. _enrich_row_sync() → enrich_by_keys / enrich_pending 공유 검증

이 테스트는 SQL 패턴 정적 분석 + SQLite 통합 테스트로 구성됩니다.
실제 PostgreSQL 인덱스 동작은 oci 서버에서 verify_enrich.py로 검증합니다.
"""

import os
import sys
import sqlite3
import inspect
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from enricher.enricher_manager import EnricherManager
from enricher.tag_extractor import TagExtractionManager


# ═══════════════════════════════════════════════════════════════════════
# 1. SQL 패턴 정적 분석 — 나쁜 패턴 회귀 방지
# ═══════════════════════════════════════════════════════════════════════

class TestEnrichPendingSQL:
    """enrich_pending()의 SELECT 쿼리가 인덱스 친화적인지 검증"""

    def test_no_jsonb_array_length_in_query(self):
        """jsonb_array_length(tags) <= 2 → 함수 호출로 인덱스 사용 불가 → 제거되었는지 확인"""
        source = inspect.getsource(EnricherManager.enrich_pending)
        # SQL 쿼리 문자열만 추출 (docstring 설명문 제외)
        # method body에서 """ 이후 실제 SQL 영역만 검사
        in_query_body = False
        for line in source.split('\n'):
            stripped = line.strip()
            # f-string SQL 시작 감지
            if stripped.startswith('f"""') or stripped.startswith('f"'):
                in_query_body = True
                continue
            if in_query_body and ('"""' in stripped or stripped == '""",'):
                in_query_body = False
                continue
            if in_query_body and 'jsonb_array_length' in line:
                raise AssertionError(
                    "❌ jsonb_array_length() found in enrich_pending SQL! "
                    "이 함수는 인덱스를 사용할 수 없어 seq scan을 유발합니다. "
                    "부분 인덱스(idx_sec_reports_tags_null) 사용을 위해 제거해야 합니다."
                )

    def test_uses_partial_index_friendly_where(self):
        """tags IS NULL OR tags = '[]' 조건이 존재하는지 확인 (부분 인덱스 매칭)"""
        source = inspect.getsource(EnricherManager.enrich_pending)
        assert "tags IS NULL" in source, (
            "❌ 'tags IS NULL' 조건이 enrich_pending에 없습니다! "
            "부분 인덱스(idx_sec_reports_tags_null) 사용을 위해 필요합니다."
        )
        # SQL 본문에 jsonb_array_length가 없는지 검증 (docstring 설명문 제외)
        in_query_body = False
        for line in source.split('\n'):
            stripped = line.strip()
            if stripped.startswith('f"""') or stripped.startswith('f"'):
                in_query_body = True
                continue
            if in_query_body and ('"""' in stripped or stripped == '""",'):
                in_query_body = False
                continue
            if in_query_body and 'jsonb_array_length' in line:
                raise AssertionError(
                    "❌ jsonb_array_length() found in enrich_pending SQL!"
                )


class TestConnectionTimeouts:
    """_get_conn()이 session-level timeout을 적용하는지 검증"""

    def test_set_statement_timeout(self):
        """모든 연결에 statement_timeout이 설정되는지 확인"""
        source = inspect.getsource(EnricherManager._get_conn)
        assert "statement_timeout" in source, (
            "❌ SET statement_timeout이 _get_conn()에 없습니다! "
            "타임아웃 없는 쿼리는 장시간 테이블락의 원인입니다."
        )
        assert "SET lock_timeout" in source or "lock_timeout" in source, (
            "❌ SET lock_timeout이 _get_conn()에 없습니다! "
            "락 타임아웃 없으면 UPDATE가 무한정 대기할 수 있습니다."
        )

    def test_enrich_pending_uses_timeout_connection(self):
        """enrich_pending이 timeout 적용된 _get_conn을 호출하는지 확인"""
        source = inspect.getsource(EnricherManager.enrich_pending)
        assert "_get_conn(" in source, (
            "❌ enrich_pending이 _get_conn()을 통해 연결을 생성하지 않습니다!"
        )


class TestMatchFnGuideSQL:
    """match_fnguide_summaries()가 락 타임아웃을 적용하는지 검증"""

    def test_has_lock_timeout(self):
        """match_fnguide_summaries에 lock_timeout 설정이 존재하는지 확인"""
        source = inspect.getsource(EnricherManager.match_fnguide_summaries)
        assert "lock_timeout" in source, (
            "❌ match_fnguide_summaries에 lock_timeout 설정이 없습니다! "
            "UPDATE JOIN 쿼리는 락 경합 위험이 매우 높습니다."
        )

    def test_uses_timeout_connection(self):
        """match_fnguide_summaries가 timeout 적용된 _get_conn을 호출하는지 확인"""
        source = inspect.getsource(EnricherManager.match_fnguide_summaries)
        assert "_get_conn(" in source, (
            "❌ match_fnguide_summaries가 _get_conn()을 통해 연결을 생성하지 않습니다!"
        )


class TestBackfillPdfUrlsSQL:
    """backfill_fnguide_pdf_urls()가 락 타임아웃을 적용하는지 검증"""

    def test_has_lock_timeout(self):
        source = inspect.getsource(EnricherManager.backfill_fnguide_pdf_urls)
        assert "lock_timeout" in source, (
            "❌ backfill_fnguide_pdf_urls에 lock_timeout 설정이 없습니다!"
        )


class TestUpdateStatements:
    """UPDATE문이 PK 기반 단일 행 갱신인지 검증 (index scan 보장)"""

    def test_update_tags_uses_pk(self):
        """_update_tags: WHERE report_id = %s → PK index scan"""
        source = inspect.getsource(EnricherManager._update_tags)
        assert "WHERE report_id = %s" in source, (
            "❌ _update_tags가 PK 기반 WHERE를 사용하지 않습니다!"
        )

    def test_update_tags_and_premium_uses_pk(self):
        """_update_tags_and_premium: WHERE report_id = %s → PK index scan"""
        source = inspect.getsource(EnricherManager._update_tags_and_premium)
        assert "WHERE report_id = %s" in source, (
            "❌ _update_tags_and_premium이 PK 기반 WHERE를 사용하지 않습니다!"
        )

    def test_update_premium_only_uses_pk(self):
        """_update_premium_only: WHERE report_id = %s → PK index scan"""
        source = inspect.getsource(EnricherManager._update_premium_only)
        assert "WHERE report_id = %s" in source, (
            "❌ _update_premium_only가 PK 기반 WHERE를 사용하지 않습니다!"
        )


# ═══════════════════════════════════════════════════════════════════════
# 2. _enrich_row_sync() 공유 로직 검증 (LLM 혼동 방지)
# ═══════════════════════════════════════════════════════════════════════

class TestSharedEnrichLogic:
    """enrich_by_keys와 enrich_pending이 _enrich_row_sync를 공유하는지 검증"""

    def test_enrich_by_keys_uses_shared_method(self):
        """enrich_by_keys가 _enrich_row_sync를 호출하는지 확인"""
        source = inspect.getsource(EnricherManager.enrich_by_keys)
        assert "_enrich_row_sync" in source, (
            "❌ enrich_by_keys가 _enrich_row_sync()를 사용하지 않습니다! "
            "코드 중복 → LLM 혼동 + 버그 발생 위험."
        )

    def test_enrich_pending_uses_shared_method(self):
        """enrich_pending이 _enrich_row_sync를 호출하는지 확인"""
        source = inspect.getsource(EnricherManager.enrich_pending)
        assert "_enrich_row_sync" in source, (
            "❌ enrich_pending이 _enrich_row_sync()를 사용하지 않습니다! "
            "코드 중복 → LLM 혼동 + 버그 발생 위험."
        )

    def test_enrich_row_sync_exists(self):
        """_enrich_row_sync 메서드가 존재하는지 확인"""
        assert hasattr(EnricherManager, '_enrich_row_sync'), (
            "❌ _enrich_row_sync() 메서드가 존재하지 않습니다! "
            "enrich_by_keys와 enrich_pending의 중복 코드 통합이 필요합니다."
        )

    def test_update_via_scraper_db_exists(self):
        """_update_via_scraper_db 헬퍼가 분리되어 있는지 확인"""
        assert hasattr(EnricherManager, '_update_via_scraper_db'), (
            "❌ _update_via_scraper_db() 헬퍼 메서드가 존재하지 않습니다!"
        )

    def test_update_via_direct_conn_exists(self):
        """_update_via_direct_conn 헬퍼가 분리되어 있는지 확인"""
        assert hasattr(EnricherManager, '_update_via_direct_conn'), (
            "❌ _update_via_direct_conn() 헬퍼 메서드가 존재하지 않습니다!"
        )


# ═══════════════════════════════════════════════════════════════════════
# 3. SQLite 통합 테스트 — enrich pipeline 정합성
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def enrichment_db() -> sqlite3.Connection:
    """enrichment 테스트용 SQLite 인메모리 DB (tags/stock_names/sector 컬럼 포함)"""
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE tbl_sec_reports (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            firm_nm TEXT DEFAULT '',
            article_title TEXT,
            key TEXT UNIQUE,
            writer TEXT DEFAULT '',
            reg_dt TEXT DEFAULT '',
            telegram_url TEXT DEFAULT '',
            fnguide_summary_id INTEGER,
            tags TEXT DEFAULT NULL,
            stock_names TEXT DEFAULT NULL,
            sector TEXT DEFAULT '',
            target_price NUMERIC,
            rating TEXT,
            revision_type TEXT,
            report_type TEXT,
            stock_tickers TEXT DEFAULT '[]'
        )
    """)

    # 태그 없는 레포트 3건
    cur.execute("""
        INSERT INTO tbl_sec_reports (report_id, firm_nm, article_title, key, tags)
        VALUES
            (1, '하나증권', '삼성전자 (005930): 목표가 110,000원 상향, 투자의견 매수', 'key_001', NULL),
            (2, 'KB증권', 'SK하이닉스 반도체 신기술 HBM 양산 본격화', 'key_002', NULL),
            (3, '신한금투', '현대차 전기차 신모델 출시 임박', 'key_003', NULL)
    """)
    conn.commit()
    return conn


def test_enrich_pending_sets_timeout():
    """enrich_pending이 _get_conn(statement_timeout=...)을 호출하는지 소스코드 검증"""
    source = inspect.getsource(EnricherManager.enrich_pending)
    assert "statement_timeout" in source, (
        "❌ enrich_pending이 statement_timeout을 설정하지 않습니다!"
    )


def test_enrich_by_keys_sets_timeout():
    """enrich_by_keys가 _get_conn(statement_timeout=...)을 호출하는지 소스코드 검증"""
    source = inspect.getsource(EnricherManager.enrich_by_keys)
    assert "statement_timeout" in source, (
        "❌ enrich_by_keys가 statement_timeout을 설정하지 않습니다!"
    )


def test_scheduler_has_idle_backoff():
    """scheduler.py에 idle backoff 로직이 존재하는지 검증"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "scheduler",
        os.path.join(os.path.dirname(__file__), "..", "enricher", "scheduler.py")
    )
    # 소스코드 직접 읽기 (import시 무한루프 진입 방지)
    with open(spec.origin) as f:
        source = f.read()
    assert "IDLE_BACKOFF" in source or "idle_backoff" in source or "current_backoff" in source, (
        "❌ scheduler.py에 idle backoff 로직이 없습니다! "
        "미처리 레포트 없을 때도 30초마다 seq scan → DB 부하."
    )


# ═══════════════════════════════════════════════════════════════════════
# 4. TagExtractionManager — 태그 추출 정합성 (기존 기능 회귀 방지)
# ═══════════════════════════════════════════════════════════════════════

def test_tag_extractor_returns_valid_structure():
    """TagExtractionManager.extract_tags_sync()가 올바른 응답 구조를 반환하는지 확인"""
    import asyncio
    manager = TagExtractionManager()

    async def run():
        return await manager.extract_tags(
            article_title="삼성전자 목표주가 120,000원 상향, 반도체 업황 개선",
            firm_nm="하나증권",
            report_id=1,
        )

    result = asyncio.run(run())
    assert result["status"] == "success"
    assert isinstance(result["tags"], list)
    assert isinstance(result["stock_names"], list)
    assert isinstance(result["sector"], str)
    assert result["model"] == "rule-based"


def test_enrich_row_sync_integration(enrichment_db):
    """_enrich_row_sync()가 태그 추출+파싱+DB 업데이트 흐름을 완료하는지 검증

    UPDATE문은 PostgreSQL %s placeholder를 사용하므로, DB 업데이트 메서드만 mock 처리합니다.
    태그 추출 → 프리미엄 파싱 → DB 업데이트 분기의 로직 흐름을 검증합니다.
    """
    from unittest.mock import MagicMock
    from enricher.premium_parser import PremiumReportParser

    manager = EnricherManager.__new__(EnricherManager)

    # 최소 속성 설정
    manager.host = "localhost"
    manager.port = "5432"
    manager.database = "test"
    manager.user = "test"
    manager.password = "test"
    manager._db = None
    manager.MAIN_TABLE = "tbl_sec_reports"
    manager.has_premium_columns = False
    manager.extractor = TagExtractionManager()
    manager.premium_parser = PremiumReportParser()

    # DB 업데이트가 호출되었는지 검증하기 위한 mock
    manager._update_tags = MagicMock()
    manager._update_tags_and_premium = MagicMock()
    manager._update_premium_only = MagicMock()

    # 기존 데이터 확인
    cur = enrichment_db.cursor()
    cur.execute("SELECT report_id, firm_nm, article_title FROM tbl_sec_reports WHERE report_id = 1")
    row = cur.fetchone()
    assert row is not None

    # _enrich_row_sync 호출 (실제 SQL은 실행하지 않고 로직 흐름만 검증)
    result = manager._enrich_row_sync(
        {"report_id": 1, "firm_nm": "하나증권",
         "article_title": "삼성전자 (005930): 목표가 110,000원 상향, 투자의견 매수"},
        enrichment_db
    )
    assert result is True, "_enrich_row_sync should return True on success"

    # _update_tags 호출 검증 (has_premium_columns=False 이므로)
    manager._update_tags.assert_called_once()
    call_args = manager._update_tags.call_args
    # 첫 번째 positional arg: conn, 두 번째: report_id
    assert call_args[0][1] == 1  # report_id
    tags_passed = call_args[0][2]  # tags
    assert isinstance(tags_passed, list)
    assert len(tags_passed) > 0, "At least one tag should be extracted"
