# syntax=docker/dockerfile:1.7
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y wget ca-certificates git && rm -rf /var/lib/apt/lists/*
RUN pip install uv

RUN groupadd --gid 1001 appgroup && useradd --uid 1001 --gid 1001 --shell /bin/bash --create-home appuser

WORKDIR /app

# ssh_library는 private repo이므로 CI 빌드에서 clone 불가.
# enricher_manager.py에 자체 fallback(BasePostgreSQLManager)이 구현되어 있어
# ssh_library가 없어도 정상 동작합니다.
# (로컬 개발 시에는 uv sync로 설치, 프로덕션은 fallback 사용)

COPY --chown=appuser:appgroup pyproject.toml uv.lock ./
RUN uv sync --frozen --no-cache

COPY --chown=appuser:appgroup enricher/ ./enricher/
COPY --chown=appuser:appgroup *.py ./

RUN mkdir -p /log && chown -R appuser:appgroup /log
USER appuser
CMD [".venv/bin/python", "enricher/scheduler.py"]
