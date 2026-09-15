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
	uv run --no-sync python -m scripts.check_repository --worktree --history
	uv run --no-sync python -m scripts.check_repository
docker-test:
	uv run --no-sync python -m scripts.smoke_docker

.PHONY: criteo-data criteo-verify
criteo-data:
	uv run --no-sync python -m scripts.prepare_criteo
criteo-verify:
	uv run --no-sync python -m scripts.evaluate_criteo --mode verify --split all --output artifacts/criteo-verify

.PHONY: criteo-expanded-verify
criteo-expanded-verify:
	uv run --no-sync python -m scripts.evaluate_criteo --suite expanded --mode verify --split all --output artifacts/criteo-expanded-verify

.PHONY: large-eval-data large-eval-verify large-eval-dev large-eval-test
large-eval-data:
	uv run --no-sync python -m scripts.build_large_eval
large-eval-verify:
	uv run --no-sync python -m scripts.verify_large_eval
large-eval-dev:
	uv run --no-sync python -m scripts.evaluate_large --split dev --limit 200 --concurrency 4 --output artifacts/criteo-large-v1-dev-pilot
large-eval-test:
	uv run --no-sync python -m scripts.evaluate_large --split test --concurrency 8 --output artifacts/criteo-large-v1-test

.PHONY: large-eval-v2-verify large-eval-v2-dev large-eval-v2-test
large-eval-v2-verify:
	uv run --no-sync python -m scripts.verify_large_eval --dataset data/external/criteo-large-v2 --output artifacts/criteo-large-v2-verify
large-eval-v2-dev:
	uv run --no-sync python -m scripts.evaluate_large --dataset data/external/criteo-large-v2 --verification artifacts/criteo-large-v2-verify --split dev --limit 1000 --concurrency 4 --output artifacts/criteo-large-v2-dev-share-v3
large-eval-v2-test:
	uv run --no-sync python -m scripts.evaluate_large --dataset data/external/criteo-large-v2 --verification artifacts/criteo-large-v2-verify --split test --concurrency 8 --output artifacts/criteo-large-v2-test
