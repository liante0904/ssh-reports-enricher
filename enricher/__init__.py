"""
Enricher - 레포트 데이터 보강 서비스 (독립 패키지)

LLM 없이 규칙 기반(Regex + 키워드 사전)으로 레포트 제목에서 태그/종목명/산업을 추출합니다.

이 패키지는 scraper에 종속되지 않는 독립 모듈로 설계되어 있으며,
추후 별도 프로젝트로 분리할 수 있습니다.

사용법:
    from enricher.tag_extractor import TagExtractionManager
    from enricher.enricher_manager import EnricherManager

    # 단독 사용 (자체 DB 연결)
    extractor = TagExtractionManager()
    result = await extractor.extract_tags(title, firm_nm, report_id)

    # DB 연동 사용
    enricher = EnricherManager()
    enricher.enrich_by_keys(keys)       # 새 레포트 즉시 태깅
    enricher.enrich_pending(limit=50)   # 미처리 배치 처리
"""

from enricher.tag_extractor import TagExtractionManager
from enricher.enricher_manager import EnricherManager

__all__ = ["TagExtractionManager", "EnricherManager"]
