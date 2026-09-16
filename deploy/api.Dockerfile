FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv && useradd --uid 10001 --create-home appuser
COPY backend/pyproject.toml backend/uv.lock /app/backend/
COPY backend/fund_kb /app/backend/fund_kb
COPY backend/alembic.ini /app/backend/alembic.ini
COPY backend/migrations /app/backend/migrations
COPY contracts /app/contracts
WORKDIR /app/backend
ENV UV_CACHE_DIR=/app/.uv-cache
RUN uv sync --frozen --no-dev && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser
EXPOSE 8765
CMD ["/app/backend/.venv/bin/uvicorn", "fund_kb.main:app", "--host", "0.0.0.0", "--port", "8765"]
