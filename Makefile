.PHONY: setup start stop api ui mcp test lint typecheck check health compose-up compose-down

setup:
	uv sync --extra dev --extra data

start:
	@trap 'kill 0' INT TERM EXIT; \
	uv run uvicorn api.main:app --reload --port 8000 & \
	uv run streamlit run ui/app.py --server.port 8501 & \
	wait

stop:
	@echo "Use Ctrl-C in the terminal running 'make start'."

api:
	uv run uvicorn api.main:app --reload --port 8000

ui:
	uv run streamlit run ui/app.py --server.port 8501

mcp:
	uv run python -m core.mcp.entrypoint

test:
	uv run pytest

lint:
	uv run ruff check .

typecheck:
	uv run mypy api core domains ui scripts local_app

check: lint typecheck test

health:
	curl --fail --silent http://localhost:8000/health

compose-up:
	docker compose up --build

compose-down:
	docker compose down
