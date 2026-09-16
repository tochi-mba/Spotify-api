.DEFAULT_GOAL := help
UV ?= uv

.PHONY: help install fmt lint type imports test test-live cov check run docker clean

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install everything
	$(UV) sync --group dev

fmt: ## Format the codebase
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

lint: ## Lint (no fixes)
	$(UV) run ruff format --check .
	$(UV) run ruff check .

type: ## Strict type check
	$(UV) run mypy

imports: ## Enforce the architectural layering contracts
	$(UV) run lint-imports

test: ## Run the test suite with 100% branch coverage enforced
	$(UV) run pytest --cov --cov-report=term-missing

test-live: ## Run the real Spotify Web API tests (needs credentials)
	$(UV) run pytest -m live --no-cov

cov: ## Write an HTML coverage report to htmlcov/
	$(UV) run pytest --cov --cov-report=html

check: lint type imports test ## Everything CI runs

run: ## Serve the API on :8007 with reload
	$(UV) run uvicorn spotify_api.app:create_app --factory --reload --port 8007

docker: ## Build the container image
	docker build -t spotify-api:local .

clean: ## Remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov .coverage coverage.xml build dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
