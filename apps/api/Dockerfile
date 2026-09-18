# ---- API build stage: build a wheel for the app ----
FROM python:3.11-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1
WORKDIR /build
RUN pip install --upgrade pip
COPY pyproject.toml ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels .

# ---- API runtime stage: minimal image with the wheel + uvicorn ----
FROM python:3.11-slim AS runtime
# Stage 11.4: build-time git SHA + service version. CI passes them via
# ``docker build --build-arg GIT_SHA=$(git rev-parse --short HEAD) \
#                       --build-arg SERVICE_VERSION=...``; the env vars
# are picked up by :class:`core.config.Settings` and surfaced through
# ``/health`` / ``/health/live`` / ``/health/ready`` so the on-call
# engineer can tell which image they're hitting.
ARG GIT_SHA=unknown
ARG SERVICE_VERSION=dev
ENV GIT_SHA=${GIT_SHA} \
    SERVICE_VERSION=${SERVICE_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels ai-customer-api \
    && rm -rf /wheels
COPY .env.example /app/.env.example
EXPOSE 8000
# Liveness-only probe — never touches the DB / Redis / Qdrant layer,
# so a dependency outage doesn't trigger pod restarts. Use
# ``/health/ready`` for the readinessProbe instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
