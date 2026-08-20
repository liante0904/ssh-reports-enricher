from pathlib import Path


ROOT = Path(__file__).parents[1]


def _function_source(name: str) -> str:
    source = (ROOT / "enricher" / "enricher_manager.py").read_text(encoding="utf-8")
    start = source.index(f"    def {name}")
    next_method = source.find("\n    def ", start + 1)
    return source[start:] if next_method == -1 else source[start:next_method]


def test_pending_enrichment_only_selects_never_processed_rows():
    query_source = _function_source("enrich_pending")

    assert "WHERE tags IS NULL" in query_source
    assert "tags = '[]'" not in query_source
    assert "tags = '[]'::jsonb" not in query_source


def test_backfill_does_not_reprocess_empty_tag_rows():
    source = (ROOT / "enricher" / "backfill_sync.py").read_text(encoding="utf-8")
    select_sql = source[source.index("SELECT_SQL"):source.index("UPDATE_SQL")]

    assert "WHERE tags IS NULL" in select_sql
    assert "tags = '[]'" not in select_sql
    assert "tags = '[]'::jsonb" not in select_sql


def test_gemini_batch_does_not_reprocess_empty_tag_rows():
    source = (ROOT / "enricher" / "gemini_tag_enrich.py").read_text(encoding="utf-8")
    select_sql = source[source.index("SELECT report_id"):source.index("UPDATE tbl_sec_reports")]

    assert "WHERE tags IS NULL" in select_sql
    assert "tags = '[]'" not in select_sql


def test_scheduler_default_matches_low_frequency_insert_cadence():
    source = (ROOT / "enricher" / "scheduler.py").read_text(encoding="utf-8")

    assert 'ENRICHER_INTERVAL_SECONDS", "1800"' in source
    assert 'ENRICHER_IDLE_BACKOFF_MIN", "1800"' in source
