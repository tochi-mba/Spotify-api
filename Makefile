.PHONY: help install test cov lint format typecheck check run live clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install the project and dev dependencies
	uv sync --all-extras

test:  ## Run the test suite (100% coverage enforced)
	uv run pytest

cov:  ## Run the suite and write an HTML coverage report
	uv run pytest --cov-report=html
	@echo "open htmlcov/index.html"

lint:  ## Check formatting and lint rules
	uv run ruff check .
	uv run ruff format --check .

format:  ## Apply formatting and safe lint fixes
	uv run ruff check --fix .
	uv run ruff format .

typecheck:  ## Run mypy in strict mode
	uv run mypy

check: lint typecheck test  ## Everything CI runs

run:  ## Start the service
	uv run python -m spotify_api

live:  ## Run the live tests against the real Spotify API
	uv run pytest -m live

clean:  ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov coverage.xml .coverage dist build
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
