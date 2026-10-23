FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --no-install-project
COPY . .
RUN uv sync --no-dev

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
