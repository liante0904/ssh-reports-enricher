#!/usr/bin/env bash
set -euo pipefail

# 전체 이력 기본 실행. 중단 후에는 예: ./scripts/backfill_fnguide_mapping.sh --start-offset 180 --end-offset 0
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec uv run python -m enricher.backfill_fnguide_mapping "$@"
