--
-- Migration: 프리미엄 금융 데이터 다차원 구조화 & 정규화 스키마 구축
-- tbl_sec_reports 데이터 보존형 컬럼 확장 및 신규 애널리스트/추정치 관계 테이블 정의
--

-- 1. 기존 tbl_sec_reports 테이블 컬럼 안전 추가 (기존 데이터 손상 없음)
ALTER TABLE public.tbl_sec_reports
  ADD COLUMN IF NOT EXISTS target_price NUMERIC,
  ADD COLUMN IF NOT EXISTS rating VARCHAR(20),
  ADD COLUMN IF NOT EXISTS revision_type VARCHAR(20),
  ADD COLUMN IF NOT EXISTS report_type VARCHAR(20),
  ADD COLUMN IF NOT EXISTS stock_tickers JSONB DEFAULT '[]'::jsonb;

-- 인덱스 추가 (조회 속도 최적화)
CREATE INDEX IF NOT EXISTS idx_tb_sec_reports_target_price ON public.tbl_sec_reports USING btree (target_price);
CREATE INDEX IF NOT EXISTS idx_tb_sec_reports_rating ON public.tbl_sec_reports USING btree (rating);
CREATE INDEX IF NOT EXISTS idx_tb_sec_reports_report_type ON public.tbl_sec_reports USING btree (report_type);
CREATE INDEX IF NOT EXISTS idx_tb_sec_reports_stock_tickers ON public.tbl_sec_reports USING gin (stock_tickers);

COMMENT ON COLUMN public.tbl_sec_reports.target_price IS '애널리스트 제시 정수형 목표주가 (원화 기준)';
COMMENT ON COLUMN public.tbl_sec_reports.rating IS '표준화된 투자의견 (BUY, HOLD, SELL, NEUTRAL)';
COMMENT ON COLUMN public.tbl_sec_reports.revision_type IS '목표주가 직전 발간 대비 변동 유형 (UPGRADE, DOWNGRADE, MAINTAIN, NEW)';
COMMENT ON COLUMN public.tbl_sec_reports.report_type IS '보고서 유형 분류 (COMPANY, INDUSTRY, MACRO, STRATEGY, QUANT)';
COMMENT ON COLUMN public.tbl_sec_reports.stock_tickers IS '6자리 한국 거래소(KRX) 표준 종목코드 배열 (예: ["005930"])';


-- 2. 신규 애널리스트 마스터 테이블 생성
CREATE TABLE IF NOT EXISTS public.tbl_analysts (
    analyst_id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    sec_firm_order INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT tbl_analysts_name_firm_unique UNIQUE (name, sec_firm_order)
);

COMMENT ON TABLE public.tbl_analysts IS '애널리스트(작성 연구원) 마스터 정보 테이블';
COMMENT ON COLUMN public.tbl_analysts.analyst_id IS '고유 애널리스트 ID';
COMMENT ON COLUMN public.tbl_analysts.name IS '애널리스트 성명';
COMMENT ON COLUMN public.tbl_analysts.sec_firm_order IS '소속 증권사 ID (tbl_sec_firm_info 참조 매핑용)';


-- 3. 신규 N:M 레포트-애널리스트 매핑 교차 테이블 생성
CREATE TABLE IF NOT EXISTS public.tbl_report_analysts (
    report_id BIGINT NOT NULL,
    analyst_id INTEGER NOT NULL,
    assigned_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (report_id, analyst_id)
);

COMMENT ON TABLE public.tbl_report_analysts IS '레포트와 공동 저술 애널리스트 간의 N:M 매핑 교차 테이블';


-- 4. 신규 실적 추정치 테이블 생성
CREATE TABLE IF NOT EXISTS public.tbl_report_forecasts (
    forecast_id SERIAL PRIMARY KEY,
    report_id BIGINT NOT NULL,
    year INTEGER NOT NULL,
    sales NUMERIC,
    operating_profit NUMERIC,
    net_income NUMERIC,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT tbl_report_forecasts_report_year_unique UNIQUE (report_id, year)
);

CREATE INDEX IF NOT EXISTS idx_tb_report_forecasts_report_id ON public.tbl_report_forecasts USING btree (report_id);

COMMENT ON TABLE public.tbl_report_forecasts IS '애널리스트 제시 개별 보고서별 미래 실적 추정치(Forecasts) 테이블';
COMMENT ON COLUMN public.tbl_report_forecasts.year IS '추정 대상 회기 연도 (예: 2026, 2027)';
COMMENT ON COLUMN public.tbl_report_forecasts.sales IS '예상 매출액 (단위: 십억원)';
COMMENT ON COLUMN public.tbl_report_forecasts.operating_profit IS '예상 영업이익 (단위: 십억원)';
COMMENT ON COLUMN public.tbl_report_forecasts.net_income IS '예상 당기순이익 (단위: 십억원)';
