"""
Test Premium Enricher - 프리미엄 금융 데이터 고도화 가공 단위 및 통합 테스트

이 테스트 코드는 PremiumReportParser의 모든 정량 추출 로직을 타이트하게 검증하고,
SQLite 인메모리 가상 DB 환경을 구축하여 프리미엄 스키마 테이블들에 데이터가 
기존 데이터 손실 및 오염 없이 안전하게 적재·관계 매핑되는지 통합 테스트를 수행합니다.
"""

import os
import sys
import sqlite3
import pytest
from typing import Generator

# 프로젝트 루트를 파이썬 모듈 검색 경로에 명시적으로 추가
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from enricher.premium_parser import PremiumReportParser

# ═══════════════════════════════════════════════════════════════════════
# 1. 단위 테스트 (Unit Tests)
# ═══════════════════════════════════════════════════════════════════════

def test_extract_tickers():
    """상장사 6자리 표준 종목코드(Ticker) 파싱 정밀 검증"""
    parser = PremiumReportParser()
    
    # 케이스 A: 괄호 안에 숫자가 명시되어 있는 경우
    title_a = "팸텍 (271830.KQ): IPO 주관사 업데이트: 반도체 주도 성장"
    assert parser.extract_tickers(title_a) == ["271830"]
    
    # 케이스 B: 괄호가 없고 오직 한글 명칭만 있는 경우 매핑 사전을 활용한 추출
    title_b = "[New K-ETF] 현대차 로보틱스 밸류체인 TOP3+"
    assert parser.extract_tickers(title_b) == ["005380"]
    
    # 케이스 C: 복수의 매칭
    title_c = "삼성전자와 SK하이닉스 반도체 전면 비교 분석"
    assert parser.extract_tickers(title_c) == ["000660", "005930"]


def test_parse_target_price_and_rating():
    """목표주가 정수 변환, 변동 방향(Action) 및 투자의견(Rating) 추출 검증"""
    parser = PremiumReportParser()
    
    # 케이스 A: 숫자가 온전하며 상향 지시
    res_a = parser.parse_target_price_and_rating("삼성전자 (005930): 목표가 110,000원으로 상향 조정, 투자의견 매수")
    assert res_a["target_price"] == 110000
    assert res_a["revision_type"] == "UPGRADE"
    assert res_a["rating"] == "BUY"
    
    # 케이스 B: 한글 '만' 단위의 믹스 및 하향 지시
    res_b = parser.parse_target_price_and_rating("네이버: 목표주가 24만 원으로 하향, HOLD의견 제시")
    assert res_b["target_price"] == 240000
    assert res_b["revision_type"] == "DOWNGRADE"
    assert res_b["rating"] == "HOLD"
    
    # 케이스 C: 목표가 유지
    res_c = parser.parse_target_price_and_rating("기아 (000270): 목표가 150,000원 유지")
    assert res_c["target_price"] == 150000
    assert res_c["revision_type"] == "MAINTAIN"


def test_parse_analysts():
    """비정형 애널리스트 연구원 텍스트의 정규화 분해 검증"""
    parser = PremiumReportParser()
    
    # 케이스 A: 콤마 구분자 및 다양한 직함 수식어 탈락 정밀 정제
    writer_a = "홍길동 연구원, 김철수 책임연구원, Analyst 임꺽정"
    assert parser.parse_analysts(writer_a) == ["홍길동", "김철수", "임꺽정"]
    
    # 케이스 B: 슬래시(/) 및 공백 구분
    writer_b = "성춘향 선임 / 이몽룡 위원"
    assert parser.parse_analysts(writer_b) == ["성춘향", "이몽룡"]


def test_classify_report_type():
    """기업코드 유무 및 키워드 우위 기반의 레포트 장르/분류 검증"""
    parser = PremiumReportParser()
    
    # 케이스 A: 기업 Ticker가 매핑될 수 있는 경우 -> COMPANY
    assert parser.classify_report_type("삼성전자 반도체 공급망 현황", tickers=["005930"]) == "COMPANY"
    
    # 케이스 B: 기업 Ticker는 없고 산업 단어가 있는 경우 -> INDUSTRY
    assert parser.classify_report_type("2차전지 배터리 양극재 산업 동향 점검") == "INDUSTRY"
    
    # 케이스 C: 거시 경제 관련 지배 단어 -> MACRO
    assert parser.classify_report_type("연준 FOMC 금리 동결 결정과 환율 전망") == "MACRO"
    
    # 케이스 D: 계량 분석 및 자산 배분 -> STRATEGY / QUANT
    assert parser.classify_report_type("2026년 하반기 모델 포트폴리오 자산배분 전략 수립") == "STRATEGY"
    assert parser.classify_report_type("퀀트 스크리닝을 통한 계량분석 실전 매매") == "QUANT"


# ═══════════════════════════════════════════════════════════════════════
# 2. 통합 관계형 DB 가상 테스트 (Integration Database Tests)
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_db() -> Generator[sqlite3.Connection, None, None]:
    """
    메모리 SQLite DB를 가동하고, 기존 데이터를 유지한 채로 
    안전하게 새로운 정규화 테이블을 얹는 스키마 통합 테스트 환경을 구축합니다.
    """
    # 1. 인메모리 DB 구동
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    
    # 2. 기존 tbl_sec_reports 오리지널 스키마 미니어처 테이블 적재 (기존 데이터)
    cur.execute("""
        CREATE TABLE tbl_sec_reports (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            firm_nm TEXT,
            article_title TEXT,
            writer TEXT,
            reg_dt TEXT
        )
    """)
    
    # 기존 레포트 데이터 인서트 시뮬레이션
    cur.execute("""
        INSERT INTO tbl_sec_reports (firm_nm, article_title, writer, reg_dt)
        VALUES ('하나증권', '팸텍 (271830.KQ): IPO 주관사 업데이트: 반도체 주도 성장', '홍길동 연구원, 김철수 선임', '2026-06-07')
    """)
    conn.commit()
    
    # 3. 마이그레이션 스텝: 기존 데이터 변경 없이 신규 프리미엄 칼럼 안전 셋업
    # SQLite ALTER TABLE 문법에 최적화하여 셋업
    cur.execute("ALTER TABLE tbl_sec_reports ADD COLUMN target_price NUMERIC")
    cur.execute("ALTER TABLE tbl_sec_reports ADD COLUMN rating VARCHAR(20)")
    cur.execute("ALTER TABLE tbl_sec_reports ADD COLUMN revision_type VARCHAR(20)")
    cur.execute("ALTER TABLE tbl_sec_reports ADD COLUMN report_type VARCHAR(20)")
    cur.execute("ALTER TABLE tbl_sec_reports ADD COLUMN stock_tickers TEXT DEFAULT '[]'") # SQLite에선 TEXT로 JSONB 시뮬레이션
    
    # 4. 신규 정규화 테이블 생성
    cur.execute("""
        CREATE TABLE tbl_analysts (
            analyst_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sec_firm_order INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (name, sec_firm_order)
        )
    """)
    
    cur.execute("""
        CREATE TABLE tbl_report_analysts (
            report_id INTEGER NOT NULL,
            analyst_id INTEGER NOT NULL,
            PRIMARY KEY (report_id, analyst_id)
        )
    """)
    
    cur.execute("""
        CREATE TABLE tbl_report_forecasts (
            forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            year INTEGER NOT NULL,
            sales NUMERIC,
            operating_profit NUMERIC,
            UNIQUE (report_id, year)
        )
    """)
    conn.commit()
    
    yield conn
    conn.close()


def test_premium_pipeline_database_integration(mock_db):
    """실제 가상 DB 환경에서 비정형 데이터 가공 후, 정형/정규화 테이블 관계적 적재 흐름 완전 통과 검증"""
    cur = mock_db.cursor()
    parser = PremiumReportParser()
    
    # 1. 기존 데이터에서 로컬 조회 (Read-Only 흐름)
    cur.execute("SELECT report_id, firm_nm, article_title, writer FROM tbl_sec_reports LIMIT 1")
    row = cur.fetchone()
    assert row is not None
    
    report_id, firm_nm, title, writer = row
    
    # 2. 프리미엄 파싱 알고리즘 작동 (On-Memory)
    tickers = parser.extract_tickers(title)
    opinion = parser.parse_target_price_and_rating(title)
    analysts = parser.parse_analysts(writer)
    rep_type = parser.classify_report_type(title, tickers)
    
    # 3. 1단계: 원래 레포트 테이블 확장 컬럼 UPDATE (안전 갱신)
    import json
    cur.execute("""
        UPDATE tbl_sec_reports
        SET target_price = ?, rating = ?, revision_type = ?, report_type = ?, stock_tickers = ?
        WHERE report_id = ?
    """, (opinion["target_price"], opinion["rating"], opinion["revision_type"], rep_type, json.dumps(tickers), report_id))
    
    # 4. 2단계: 신규 애널리스트 마스터 테이블 중복 배제 INSERT 및 N:M 매핑 적재
    for name in analysts:
        # INSERT OR IGNORE 로 중복 방지 적재 시뮬레이션
        cur.execute("""
            INSERT OR IGNORE INTO tbl_analysts (name, sec_firm_order)
            VALUES (?, ?)
        """, (name, 11)) # 하나증권 분류코드 예시 11
        
        # 방금 인서트되었거나 기존에 있던 ID 가져오기
        cur.execute("SELECT analyst_id FROM tbl_analysts WHERE name = ? AND sec_firm_order = ?", (name, 11))
        analyst_id = cur.fetchone()[0]
        
        # 교차 테이블 적재
        cur.execute("""
            INSERT OR IGNORE INTO tbl_report_analysts (report_id, analyst_id)
            VALUES (?, ?)
        """, (report_id, analyst_id))
        
    # 5. 3단계: 신규 실적 추정치 적재 (Gemini 연동 시뮬레이션)
    # 가상 2026년 실적 추정치 대입 (매출: 500억, 영업이익: 80억)
    cur.execute("""
        INSERT OR IGNORE INTO tbl_report_forecasts (report_id, year, sales, operating_profit)
        VALUES (?, ?, ?, ?)
    """, (report_id, 2026, 50, 8))
    mock_db.commit()
    
    # ═══════════════════════════════════════════════════════════════════
    # 최종 무오염 및 정규화 무결성 최종 검증
    # ═══════════════════════════════════════════════════════════════════
    
    # A. 원본 테이블 보존 및 메타데이터 업데이트 성공여부 조회
    cur.execute("SELECT target_price, rating, report_type, stock_tickers FROM tbl_sec_reports WHERE report_id = ?", (report_id,))
    tp, rat, rt, tk_str = cur.fetchone()
    assert tp is None # 본 제목("팸텍 IPO 주관사...")에는 목표주가 숫자가 없으므로 None이 맞음
    assert rat == "BUY" # 기본 의견 BUY 표준화 성공
    assert rt == "COMPANY" # '팸텍' Ticker 매핑 성공으로 회사 레포트 판정 통과
    assert json.loads(tk_str) == ["271830"] # 표준 Ticker "271830" 결합 성공
    
    # B. 애널리스트 다중 적재 및 N:M 매핑 정규화 통과 여부 검증
    cur.execute("""
        SELECT a.name 
        FROM tbl_analysts a
        JOIN tbl_report_analysts ra ON a.analyst_id = ra.analyst_id
        WHERE ra.report_id = ?
    """, (report_id,))
    mapped_analysts = [r[0] for r in cur.fetchall()]
    assert len(mapped_analysts) == 2
    assert "홍길동" in mapped_analysts
    assert "김철수" in mapped_analysts
    
    # C. 예상 추정 실적 데이터 정상 조인 검증
    cur.execute("SELECT year, sales, operating_profit FROM tbl_report_forecasts WHERE report_id = ?", (report_id,))
    f_year, f_sales, f_op = cur.fetchone()
    assert f_year == 2026
    assert f_sales == 50
    assert f_op == 8
