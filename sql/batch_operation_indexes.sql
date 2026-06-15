--
-- Migration: 배치 작업(enrich_pending, FnGuide 매칭, PDF 역매칭) 성능 최적화 및 테이블락 방지 인덱스
--
-- 문제: enrich_pending SELECT → tags 컬럼 seq scan (280K+ rows)
--       match_fnguide_summaries UPDATE JOIN → fnguide_summary_id, firm_nm seq scan
--       backfill_fnguide_pdf_urls → fnguide_summary_id 조인 seq scan
--
-- 해결: 부분 인덱스 + B-tree 인덱스 + 필요시 pg_trgm 확장
--

-- 0. pg_trgm 확장 (이미 설치되어 있을 수 있음, LIKE '%keyword%' 최적화용)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 1. tbl_sec_reports: 태그 미처리 레포트 빠른 조회용 부분 인덱스
--    enrich_pending()의 WHERE + ORDER BY 최적화
CREATE INDEX IF NOT EXISTS idx_sec_reports_tags_null
    ON public.tbl_sec_reports (report_id DESC)
    WHERE (tags IS NULL OR tags = '[]'::jsonb);

-- 2. tbl_sec_reports: fnguide_summary_id 참조 무결성 + JOIN 성능
CREATE INDEX IF NOT EXISTS idx_sec_reports_fnguide_summary_id
    ON public.tbl_sec_reports USING btree (fnguide_summary_id);

-- 3. tbl_sec_reports: firm_nm JOIN (FnGuide 매칭 provider 비교)
CREATE INDEX IF NOT EXISTS idx_sec_reports_firm_nm
    ON public.tbl_sec_reports USING btree (firm_nm);

-- 4. tbl_sec_reports: article_title LIKE 검색 최적화 (FnGuide 매칭 company_name 포함)
CREATE INDEX IF NOT EXISTS idx_sec_reports_article_title_trgm
    ON public.tbl_sec_reports USING gin (article_title gin_trgm_ops);

-- 5. tbl_sec_reports: writer LIKE 검색 최적화 (FnGuide 매칭 author 포함)
CREATE INDEX IF NOT EXISTS idx_sec_reports_writer_trgm
    ON public.tbl_sec_reports USING gin (writer gin_trgm_ops);

-- 6. tbl_fnguide_report_summaries: provider JOIN 최적화
CREATE INDEX IF NOT EXISTS idx_fnguide_summaries_provider
    ON public.tbl_fnguide_report_summaries USING btree (provider);

-- 7. tbl_fnguide_report_summaries: author LIKE 검색 최적화
CREATE INDEX IF NOT EXISTS idx_fnguide_summaries_author_trgm
    ON public.tbl_fnguide_report_summaries USING gin (author gin_trgm_ops);

-- 8. tbl_fnguide_report_summaries: company_name LIKE 검색 최적화
CREATE INDEX IF NOT EXISTS idx_fnguide_summaries_company_name_trgm
    ON public.tbl_fnguide_report_summaries USING gin (company_name gin_trgm_ops);

-- 9. tbl_fnguide_report_summaries: pdf_url NULL 체크 빠른 필터 (backfill 용)
CREATE INDEX IF NOT EXISTS idx_fnguide_summaries_pdf_url_null
    ON public.tbl_fnguide_report_summaries (summary_id)
    WHERE (pdf_url IS NULL OR pdf_url = '');

COMMENT ON INDEX public.idx_sec_reports_tags_null IS 'enrich_pending 미처리 레포트 부분 인덱스 (report_id DESC 정렬 포함)';
COMMENT ON INDEX public.idx_sec_reports_fnguide_summary_id IS 'FnGuide 매칭 및 PDF 역매칭 JOIN 최적화';
COMMENT ON INDEX public.idx_sec_reports_firm_nm IS '증권사명 기준 매칭 JOIN 최적화';
COMMENT ON INDEX public.idx_sec_reports_article_title_trgm IS '제목 LIKE 포함 검색 (pg_trgm)';
COMMENT ON INDEX public.idx_sec_reports_writer_trgm IS '작성자 LIKE 포함 검색 (pg_trgm)';
COMMENT ON INDEX public.idx_fnguide_summaries_provider IS '증권사명 JOIN 매칭';
COMMENT ON INDEX public.idx_fnguide_summaries_author_trgm IS '작성자 LIKE 포함 매칭 (pg_trgm)';
COMMENT ON INDEX public.idx_fnguide_summaries_company_name_trgm IS '기업명 LIKE 포함 매칭 (pg_trgm)';
COMMENT ON INDEX public.idx_fnguide_summaries_pdf_url_null IS 'backfill PDF URL 미처리 행 빠른 필터';
