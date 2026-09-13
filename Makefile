.PHONY: setup seed index run test lint format eval demo spike ui-test experiment benchmark privacy docker-test
setup:
	uv sync --python 3.12 --frozen --extra youtu --extra storage
	uv run python -m scripts.seed_data
	uv run python -m scripts.build_index
seed:
	uv run python -m scripts.seed_data
index:
	uv run python -m scripts.build_index
run:
	uv run uvicorn app.api.main:app --host 127.0.0.1 --port 8000
test:
	uv run pytest
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
format:
	uv run ruff check --fix .
	uv run ruff format .
eval:
	uv run python -m scripts.evaluate --mode demo
demo:
	uv run python -m scripts.demo
spike:
	uv run python -m scripts.model_probe
ui-test:
	uv run --extra youtu --group browser python -m scripts.smoke_ui
experiment:
	uv run python -m scripts.experiment --mode demo
benchmark:
	uv run python -m scripts.benchmark
privacy:
	uv run --no-sync python -m scripts.check_repository --worktree --history --history-ref HEAD
docker-test:
	uv run --no-sync python -m scripts.smoke_docker
