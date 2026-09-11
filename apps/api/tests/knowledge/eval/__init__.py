"""Offline RAG evaluation package (Task 6.14).

Contains the synthetic eval dataset + a runner that scores the knowledge
base retrieval pipeline. See ``run_eval.py`` for the entry point and
``rag_eval_set.json`` for the dataset.

The eval is intentionally framework-agnostic: it can be invoked from
the CLI (``python -m tests.knowledge.eval.run_eval``) or from a pytest
test (``test_rag_eval.py``). The dataset is plain JSON so the corpus is
version-controlled alongside the code that scores it.
"""
