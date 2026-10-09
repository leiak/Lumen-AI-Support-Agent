# M1 Makefile for the AI Customer API
#
# Minimal scaffolding — add targets as the project grows. The
# ``eval-rag`` target runs the offline RAG evaluation against
# the synthetic dataset in tests/knowledge/eval/. ``eval-rag-real``
# is Stage 10.3 — uses the production embedding backend (Doubao /
# OpenAI per ``DEFAULT_EMBEDDING_MODEL``) and sweeps a cosine-
# similarity threshold grid to recommend a value for
# ``RAGService.DEFAULT_SCORE_THRESHOLD``.

POETRY ?= poetry

.PHONY: eval-rag
eval-rag: ## Run the offline RAG eval against the synthetic dataset (hash-seeded mock embeddings)
	$(POETRY) run python -m tests.knowledge.eval.run_eval \
		--dataset tests/knowledge/eval/rag_eval_set.json \
		--output-dir ./eval_output

.PHONY: eval-rag-real
eval-rag-real: ## Run RAG eval with REAL embeddings + threshold sweep (Stage 10.3)
	$(POETRY) run python -m tests.knowledge.eval.run_eval \
		--dataset tests/knowledge/eval/rag_eval_set.json \
		--output-dir ./eval_output_real \
		--real-embeddings \
		--threshold-sweep \
		--top-k 10

.PHONY: eval-rag-test
eval-rag-test: ## Run the eval as a single pytest test (asserts M1 framework contract)
	$(POETRY) run pytest tests/knowledge/integration/test_rag_eval.py -v -m integration

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Task 12 (nitpick DX P2): common developer workflow targets. Additive —
# existing eval-rag* targets above are preserved for backwards compatibility.
# Run from the apps/api/ directory so apps/api/.env + uv venv are picked up.
# ---------------------------------------------------------------------------

.PHONY: install test test-int lint format type-check migrate seed clean run

install: ## Install API + dev dependencies into the active environment
	pip install -e ".[dev]"

test: ## Run unit tests (skip integration markers)
	python -m pytest -q -m "not integration" -p no:cacheprovider

test-int: ## Run integration tests (requires docker compose services up)
	python -m pytest -q -m integration -p no:cacheprovider

lint: ## Ruff lint check on src + tests
	ruff check src tests

format: ## Ruff auto-format src + tests
	ruff format src tests

type-check: ## mypy strict on src/
	mypy

migrate: ## Apply alembic migrations to HEAD (DATABASE_URL from .env)
	alembic upgrade head

seed: ## Idempotent demo seed (tenant + users + channel + budget/credit/usage)
	python ../../tests/e2e/scripts/seed.py && \
		python ../../tests/e2e/scripts/seed_budget.py

clean: ## Remove local caches (.pytest_cache / .ruff_cache / .mypy_cache / __pycache__)
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

run: ## Run the API locally (reload on change)
	uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

.DEFAULT_GOAL := help
