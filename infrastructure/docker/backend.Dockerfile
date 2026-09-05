FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY backend ./backend
COPY workers ./workers
COPY migrations ./migrations
COPY alembic.ini ./
COPY infrastructure/monitoring/logging.json ./logging.json
RUN pip install --no-cache-dir uv==0.10.9 && uv sync --frozen --no-dev \
    && useradd --uid 10001 --create-home medrag
ENV PATH="/app/.venv/bin:$PATH"
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--log-config", "logging.json"]
