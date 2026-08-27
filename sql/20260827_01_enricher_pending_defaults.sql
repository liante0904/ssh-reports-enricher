-- 신규 레포트가 enricher 대기열에 들어가도록 enrichment 필드의
-- DB 기본값을 NULL로 유지한다.
-- [] / ''은 enricher가 처리한 결과(추출 결과가 비어 있는 경우 포함)다.

ALTER TABLE public.tbl_sec_reports
    ALTER COLUMN tags DROP DEFAULT,
    ALTER COLUMN stock_names DROP DEFAULT,
    ALTER COLUMN sector DROP DEFAULT;

COMMENT ON COLUMN public.tbl_sec_reports.tags IS
    'Enricher pending 상태는 NULL, 처리 완료 결과는 JSON 배열';
COMMENT ON COLUMN public.tbl_sec_reports.stock_names IS
    'Enricher pending 상태는 NULL, 처리 완료 결과는 JSON 배열';
COMMENT ON COLUMN public.tbl_sec_reports.sector IS
    'Enricher pending 상태는 NULL, 처리 완료 결과는 문자열(없으면 빈 문자열)';
