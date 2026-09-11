# M1 Makefile for the AI Customer API
#
# Minimal scaffolding — add targets as the project grows. The
# ``eval-rag`` target runs the offline RAG evaluation against
# the synthetic dataset in tests/knowledge/eval/.

POETRY ?= poetry

.PHONY: eval-rag
eval-rag: ## Run the offline RAG eval against the synthetic dataset
	$(POETRY) run python -m tests.knowledge.eval.run_eval \
		--dataset tests/knowledge/eval/rag_eval_set.json \
		--output-dir ./eval_output

.PHONY: eval-rag-test
eval-rag-test: ## Run the eval as a single pytest test (asserts M1 framework contract)
	$(POETRY) run pytest tests/knowledge/integration/test_rag_eval.py -v -m integration

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
