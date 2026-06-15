--
-- Migration: 배치 작업(enrich_pending, FnGuide 매칭, PDF 역매칭) 성능 최적화 및 테이블락 방지 인덱스
--
-- 운영 현황 (oci, 2026-06-16):
--   tbl_sec_reports:              954 MB, 292K rows, 985K seq scans 누적
--   tbl_fnguide_report_summaries:  25 MB,  10K rows
--   tags = '[]'::jsonb: 286,702건 (98.4%)
--   pg_trgm: 미설치
--
-- 실행: oci 서버에서 psql로 직접 실행 (트랜잭션 블록 없이)
--   docker exec -i main-postgres psql -U ssh_reports_hub -d ssh_reports_hub < sql/batch_operation_indexes.sql
--
-- 주의: CREATE INDEX CONCURRENTLY는 트랜잭션 내에서 실행 불가. IF NOT EXISTS도 불가.
--       각 명령을 개별 실행하거나, psql -v ON_ERROR_STOP=1 --single-transaction 없이 실행.
--

-- ============================================================================
-- PHASE 0: pg_trgm 확장 설치 (LIKE '%keyword%' 최적화 필수)
-- ============================================================================
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================================
-- PHASE 1: enrich_pending() seq scan 제거 — 최우선 (286K건 빈 tags 필터)
--   효과: 985K seq scans → index-only scan
--   시간: ~3-5초 예상 (292K rows, CONCURRENTLY)
-- ============================================================================
-- 기존 idx_tb_sec_reports_tags (GIN)은 containment(@>) 전용 — '=' 비교에 무용
-- 부분 인덱스로 tags = '[]'::jsonb 인 행만 인덱싱 (전체의 98.4%)
CREATE INDEX CONCURRENTLY idx_sec_reports_tags_empty
    ON public.tbl_sec_reports (report_id DESC)
    WHERE tags = '[]'::jsonb;

COMMENT ON INDEX public.idx_sec_reports_tags_empty
    IS 'enrich_pending: 빈 tags 필터 (부분 인덱스, report_id DESC 정렬 포함)';

-- ============================================================================
-- PHASE 2: FnGuide 매칭 JOIN 최적화
--   match_fnguide_summaries(): r.firm_nm = s.provider JOIN
-- ============================================================================

-- 2a. tbl_fnguide_report_summaries.provider (신규 — 기존에 없음)
CREATE INDEX CONCURRENTLY idx_fnguide_summaries_provider
    ON public.tbl_fnguide_report_summaries USING btree (provider);

COMMENT ON INDEX public.idx_fnguide_summaries_provider
    IS 'FnGuide 매칭: 증권사명 JOIN (r.firm_nm = s.provider)';

-- ============================================================================
-- PHASE 3: FnGuide 매칭 LIKE '%keyword%' 검색 최적화 (pg_trgm)
--   match_fnguide_summaries():
--     s.author     LIKE r.writer || '%'     → writer_trgm
--     r.article_title LIKE '%' || s.company_name || '%' → article_title_trgm
--   시간: 각 ~10-30초 예상 (GIN trigram 인덱스는 무거움, CONCURRENTLY로 논블로킹)
-- ============================================================================

-- 3a. tbl_sec_reports.writer (기존 btree idx_reports_writer는 prefix 검색만 가능)
CREATE INDEX CONCURRENTLY idx_sec_reports_writer_trgm
    ON public.tbl_sec_reports USING gin (writer gin_trgm_ops);

COMMENT ON INDEX public.idx_sec_reports_writer_trgm
    IS 'FnGuide 매칭: author LIKE writer% 패턴 (pg_trgm)';

-- 3b. tbl_sec_reports.article_title
CREATE INDEX CONCURRENTLY idx_sec_reports_article_title_trgm
    ON public.tbl_sec_reports USING gin (article_title gin_trgm_ops);

COMMENT ON INDEX public.idx_sec_reports_article_title_trgm
    IS 'FnGuide 매칭: article_title LIKE %company_name% 패턴 (pg_trgm)';

-- 3c. tbl_fnguide_report_summaries.author
CREATE INDEX CONCURRENTLY idx_fnguide_summaries_author_trgm
    ON public.tbl_fnguide_report_summaries USING gin (author gin_trgm_ops);

COMMENT ON INDEX public.idx_fnguide_summaries_author_trgm
    IS 'FnGuide 매칭: author LIKE writer% 패턴 (pg_trgm)';

-- 3d. tbl_fnguide_report_summaries.company_name
CREATE INDEX CONCURRENTLY idx_fnguide_summaries_company_name_trgm
    ON public.tbl_fnguide_report_summaries USING gin (company_name gin_trgm_ops);

COMMENT ON INDEX public.idx_fnguide_summaries_company_name_trgm
    IS 'FnGuide 매칭: article_title LIKE %company_name% 패턴 (pg_trgm)';

-- ============================================================================
-- PHASE 4: backfill_fnguide_pdf_urls() 최적화
--   UPDATE tbl_fnguide_report_summaries SET pdf_url WHERE pdf_url IS NULL
-- ============================================================================
CREATE INDEX CONCURRENTLY idx_fnguide_summaries_pdf_url_null
    ON public.tbl_fnguide_report_summaries (summary_id)
    WHERE (pdf_url IS NULL OR pdf_url = '');

COMMENT ON INDEX public.idx_fnguide_summaries_pdf_url_null
    IS 'PDF URL 역매칭: pdf_url 미처리 행 빠른 필터 (부분 인덱스)';

-- ============================================================================
-- PHASE 5: 검증
-- ============================================================================

-- 생성된 인덱스 확인
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename IN ('tbl_sec_reports', 'tbl_fnguide_report_summaries')
  AND indexname LIKE 'idx_sec_reports_tags_empty%'
     OR indexname LIKE 'idx_fnguide_summaries_provider%'
     OR indexname LIKE '%_trgm%'
     OR indexname LIKE 'idx_fnguide_summaries_pdf_url_null%'
ORDER BY tablename, indexname;

-- 유효하지 않은(invalid) 인덱스 확인 (CONCURRENTLY 실패 시 INVALID로 남음)
SELECT indexrelid::regclass, indrelid::regclass, indisvalid
FROM pg_index
WHERE indexrelid::regclass::text LIKE 'idx_sec_reports_tags_empty%'
   OR indexrelid::regclass::text LIKE 'idx_fnguide_summaries_provider%'
   OR indexrelid::regclass::text LIKE '%_trgm%'
   OR indexrelid::regclass::text LIKE 'idx_fnguide_summaries_pdf_url_null%';
