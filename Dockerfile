# syntax=docker/dockerfile:1.7
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y wget ca-certificates git && rm -rf /var/lib/apt/lists/*
RUN pip install uv

RUN groupadd --gid 1001 appgroup && useradd --uid 1001 --gid 1001 --shell /bin/bash --create-home appuser

WORKDIR /app

# ssh_library를 git에서 clone (경로 의존성 대신)
ARG SSH_LIBRARY_REPO=https://github.com/liante0904/ssh-library.git
ARG SSH_LIBRARY_REF=main
RUN git clone --depth 1 --branch ${SSH_LIBRARY_REF} ${SSH_LIBRARY_REPO} /tmp/ssh_library \
    && cd /tmp/ssh_library && uv pip install --system -e .

COPY --chown=appuser:appgroup pyproject.toml uv.lock ./
RUN uv sync --frozen --no-cache

COPY --chown=appuser:appgroup enricher/ ./enricher/
COPY --chown=appuser:appgroup *.py ./

RUN mkdir -p /log && chown -R appuser:appgroup /log
USER appuser
CMD [".venv/bin/python", "enricher/scheduler.py"]
