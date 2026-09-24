FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
WORKDIR /app/backend
RUN pip install --no-cache-dir uv==0.11.17 && useradd --uid 10001 --create-home appuser
COPY backend/pyproject.toml backend/uv.lock /app/backend/
COPY backend/fund_kb /app/backend/fund_kb
COPY backend/alembic.ini /app/backend/alembic.ini
COPY backend/migrations /app/backend/migrations
COPY contracts /app/contracts
COPY evals/rag/schema.json /app/evals/rag/schema.json
COPY scripts/evaluate-rag.py /app/scripts/evaluate-rag.py
COPY deploy/production/ops /app/ops
ENV UV_CACHE_DIR=/tmp/uv-cache PYTHONPATH=/app/backend:/app/ops PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN uv sync --frozen --no-dev --extra semantic --extra local-models && mkdir -p /app/data && chown appuser:appuser /app/data
USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["/app/backend/.venv/bin/python", "/app/ops/entry.py"]
CMD ["api"]
