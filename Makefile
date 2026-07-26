# D.H.R.U.V.A developer entry points.
# Every target here is also a CI step: what you run locally is what gates a merge.

.DEFAULT_GOAL := help
SHELL := /bin/bash
BACKEND := backend

.PHONY: help setup hooks fmt lint types boundaries adr test cov bench check up down logs psql redis lock clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtualenv and install all dependency groups
	uv sync --project $(BACKEND) --all-groups

hooks: setup ## Install git pre-commit hooks
	uv run --project $(BACKEND) pre-commit install

fmt: ## Format the backend
	uv run --project $(BACKEND) ruff format $(BACKEND)
	uv run --project $(BACKEND) ruff check --fix $(BACKEND)

lint: ## Lint without modifying files
	uv run --project $(BACKEND) ruff check $(BACKEND)
	uv run --project $(BACKEND) ruff format --check $(BACKEND)

types: ## Strict type check (ADR-024)
	uv run --project $(BACKEND) mypy

boundaries: ## Enforce architecture: layers + bounded contexts (ADR-001)
	uv run --project $(BACKEND) lint-imports --config $(BACKEND)/.importlinter
	uv run --project $(BACKEND) dhruva-check-boundaries

adr: ## Verify decision-log integrity and immutability (ADR-027)
	uv run --project $(BACKEND) dhruva-adr-guard

adr-register: ## Register checksums for newly added ADRs
	uv run --project $(BACKEND) dhruva-adr-guard --update

test: ## Run the test suite
	uv run --project $(BACKEND) pytest

cov: ## Run the test suite with the coverage gate
	uv run --project $(BACKEND) pytest --cov --cov-report=term-missing --cov-report=xml

bench: ## Run the performance budget suite (ADR-036); excluded from `make test`
	uv run --project $(BACKEND) pytest -m benchmark -p no:randomly

lock: ## Regenerate the hash-pinned lockfiles (ADR-032)
	cd $(BACKEND) && uv pip compile pyproject.toml --python-version 3.12 --universal \
		--generate-hashes --all-extras -o requirements.lock
	cd $(BACKEND) && uv pip compile pyproject.toml --python-version 3.12 --universal \
		--generate-hashes --group dev -o requirements-dev.lock

check: lint types boundaries adr cov ## Everything CI runs, in CI's order

up: ## Start the local data services
	docker compose -f infra/docker-compose.yml up -d

down: ## Stop the local data services
	docker compose -f infra/docker-compose.yml down

logs: ## Tail the local data services
	docker compose -f infra/docker-compose.yml logs -f

psql: ## Open a psql shell against the local database
	docker compose -f infra/docker-compose.yml exec postgres psql -U dhruva -d dhruva

redis: ## Open a redis-cli shell against the local cache
	docker compose -f infra/docker-compose.yml exec redis redis-cli

clean: ## Remove caches and build artefacts
	rm -rf .mypy_cache .ruff_cache .pytest_cache .hypothesis htmlcov coverage.xml .coverage*
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
