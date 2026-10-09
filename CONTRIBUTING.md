# Contributing to Lumen AI Support Agent

Welcome! This document is the one-stop onboarding doc for new contributors.

## Quick start

```bash
git clone <repo>
cd 0401-ai-customer
# Start Postgres / Redis / Qdrant / MinIO via docker compose
docker compose -f deploy/docker-compose.yml up -d
# Install API dependencies (uv-managed venv under apps/api/.venv)
cd apps/api
uv sync --extra dev          # or: pip install -e ".[dev]"
# Run the API
make run  # or: uvicorn src.main:app --reload --port 8000
# Run tests
make test
```

For the high-level architecture and project status, see [README.md](README.md)
and `docs/superpowers/specs/2026-09-10-ai-customer-service-design.md`.

## Development workflow

- Tests: `make test` (unit), `make test-int` (integration)
- Lint: `make lint` (ruff)
- Format: `make format` (ruff format)
- Type-check: `make type-check` (mypy strict)
- Database: `make migrate` (alembic upgrade head), `make seed` (demo data)
- RAG eval: `make eval-rag` (synthetic), `make eval-rag-real` (production embeddings)

## Code style

- Python 3.11+, type hints everywhere (mypy `--strict` is enforced)
- 100-char line length (ruff default — see `pyproject.toml [tool.ruff]`)
- PEP 8 naming (ruff `N` rules)
- Use `structlog` for logging (not stdlib `logging`); see `src/core/logging.py`
- PII discipline: never log customer text; log opaque IDs only
- Commit messages: `<type>(scope): <subject>` — e.g. `fix(agent): handle empty tool_call list`

## Adding a new LLM provider

1. Create `apps/api/src/llm_client/providers/<name>_provider.py` implementing the `LLMClient` interface
2. Register it in `apps/api/src/llm_client/gateway.py::ProviderRegistry`
3. Add integration tests in `apps/api/tests/llm_client/test_<name>_provider.py`
4. Update `apps/api/.env.example` with the new env vars

## Adding a new channel adapter

1. Create `apps/api/src/channel/<name>/` with `webhook.py` and `outbound.py`
2. Wire into `apps/api/src/main.py::app.include_router(...)`
3. Add tests in `apps/api/tests/channel/test_<name>.py`
4. Update `README.md` with the new channel

## Pull request checklist

- [ ] Tests pass locally (`make test`)
- [ ] Lint + type-check pass (`make lint && make type-check`)
- [ ] No new pre-existing ruff violations
- [ ] If you changed settings, update `.env.example` and `Settings` docstrings
- [ ] If you changed the public API, update `README.md`
- [ ] If you added a new metric, add a docstring explaining the cardinality rationale
