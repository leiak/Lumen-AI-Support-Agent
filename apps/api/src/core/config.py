from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import Environment


def _parse_csv(value: str | list[str] | None) -> list[str]:
    """Parse a comma-separated env var into a clean list of strings.

    Empty strings are dropped. Whitespace is stripped. This lets us accept
    both ``WIDGET_ALLOWED_ORIGINS_GLOBAL="http://a.com, http://b.com"``
    and a pre-parsed list (used by tests).
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [v.strip() for v in value if v and v.strip()]
    return [v.strip() for v in value.split(",") if v.strip()]

# Known dev/test secret patterns. The actual .env example uses the second
# one. Production must reject any of these to prevent the
# classic "deployed with .env default" footgun.
_KNOWN_DEV_SECRETS: frozenset[str] = frozenset(
    {
        "dev-secret",
        "dev-secret-please-change-in-production-32chars",
        "test-secret-32-chars-minimum-length",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Environment = Environment.DEVELOPMENT

    # Database
    database_url: str = Field(alias="DATABASE_URL")
    database_pool_size: int = 10
    database_max_overflow: int = 20

    # Redis
    redis_url: str = Field(alias="REDIS_URL")

    # Vector DB
    qdrant_url: str = Field(default="http://localhost:6333", alias="QDRANT_URL")
    qdrant_api_key: str | None = Field(default=None, alias="QDRANT_API_KEY")

    # Auth
    jwt_secret: str = Field(alias="JWT_SECRET")
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_minutes: int = 60

    # LLM (M1: 最小客户端)
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    ollama_base_url: str | None = Field(default=None, alias="OLLAMA_BASE_URL")
    default_llm_model: str = Field(default="claude-3-5-sonnet-20241022", alias="DEFAULT_LLM_MODEL")

    # Embedding model used for KB indexing + retrieval. Default mirrors
    # OpenAI's text-embedding-3-small (1536 dim, cosine). The matching
    # vector dimension is resolved by ``knowledge.qdrant_client.vector_size_for_model``
    # at collection-create time — adding a new model requires extending
    # that dispatch (the failure mode is loud, not silent).
    default_embedding_model: str = Field(
        default="text-embedding-3-small",
        alias="DEFAULT_EMBEDDING_MODEL",
    )

    # MiniMax (OpenAI-compatible). When MINIMAX_API_KEY is set the default LLM
    # factory prefers MiniMax over Anthropic/OpenAI.
    minimax_api_key: str | None = Field(default=None, alias="MINIMAX_API_KEY")
    minimax_base_url: str | None = Field(default=None, alias="MINIMAX_BASE_URL")
    minimax_model: str | None = Field(default=None, alias="MINIMAX_MODEL")

    # Doubao / Volcano Engine Ark (OpenAI-compatible). When DOUBAO_API_KEY
    # is set the embedding client routes through ``doubao_base_url`` instead
    # of OpenAI's default. Use ``doubao-embedding`` (1024 dim) or
    # ``doubao-embedding-large`` (2048 dim) via ``DEFAULT_EMBEDDING_MODEL``.
    # If DOUBAO_API_KEY is unset the client falls back to ``openai_api_key``.
    doubao_api_key: str | None = Field(default=None, alias="DOUBAO_API_KEY")
    doubao_base_url: str = Field(
        default="https://ark.cn-beijing.volces.com/api/v3",
        alias="DOUBAO_BASE_URL",
    )

    # Observability
    log_level: str = "INFO"
    service_name: str = "ai-customer-api"

    # Stage 11.4: build-time service version. The Dockerfile passes
    # ``--build-arg GIT_SHA=$(git rev-parse --short HEAD)`` which surfaces
    # here as ``SERVICE_VERSION``; the default ``dev`` keeps local
    # ``uvicorn main:app`` runs from printing a misleading "0.1.0" while
    # an unreleased commit is in flight.
    service_version: str = Field(default="dev", alias="SERVICE_VERSION")

    # Build-time git sha. ``GIT_SHA`` is injected by the Dockerfile; not
    # exposed via /health (the version label is enough for triage).
    git_sha: str | None = Field(default=None, alias="GIT_SHA")

    # Stage 14 / Task 7 — real-time QA judge configuration.
    #
    # ``qa_judge_model`` default is a placeholder (the M2.B rollout will
    # pick the actual model). The Judge client MUST be configured with
    # a provider that ``LLMClient`` knows how to build; only the
    # well-known provider ids ("minimax" / "anthropic") are valid until
    # the routing layer ships (see llm_client/client.py with_config
    # stub).
    qa_judge_model: str = Field(default="minimax-m2.7-highspeed", alias="QA_JUDGE_MODEL")
    qa_judge_provider: str = Field(default="minimax", alias="QA_JUDGE_PROVIDER")
    qa_score_threshold_alert: float = Field(default=0.3, alias="QA_SCORE_THRESHOLD_ALERT")
    qa_judge_timeout_seconds: float = Field(default=10.0, alias="QA_JUDGE_TIMEOUT_SECONDS")
    qa_judge_max_retries: int = Field(default=1, alias="QA_JUDGE_MAX_RETRIES")

    # Stage 16 / M2.B — AWS / SES outbound credentials. The inbound webhook
    # resolves the tenant + EmailChannel by ``to_address``; outbound uses
    # these credentials to call SES SendEmail v2 directly (no boto3).
    # ``aws_*_id`` / ``aws_*_key`` default to empty strings so the API
    # still boots without credentials — outbound will simply fail at
    # send time, which is the right failure mode for a demo / CI env.
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
    aws_access_key_id: str = Field(default="", alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str = Field(default="", alias="AWS_SECRET_ACCESS_KEY")
    ses_from_address: str = Field(default="support@demo.test", alias="SES_FROM_ADDRESS")
    # Demo fallback for the email inbound webhook when no X-Tenant-ID
    # header is supplied. Production wires this via the EmailChannel
    # row's ``config_json["tenant_id"]`` lookup; this default keeps the
    # single-tenant demo path working.
    default_tenant_id: str = Field(default="", alias="DEFAULT_TENANT_ID")

    # CORS / widget origin allowlist (M1: single global allowlist).
    # Applied to both the HTTP CORS middleware and the WebSocket origin
    # check in `widget/ws/router.py`. Production must override this via
    # env var with the customer's actual embedding host(s).
    #
    # Declared as ``str`` so pydantic-settings doesn't try to JSON-decode
    # the comma-separated env value; we split it into a list ourselves.
    widget_allowed_origins_global_raw: str = Field(
        default="http://localhost:5173,http://localhost:3000",
        alias="WIDGET_ALLOWED_ORIGINS_GLOBAL",
    )

    # Stage 17 / M2.B — multimodal KB (PNG/JPG/WebP/PDF) foundation.
    #
    # ``doubao_vision_*`` drives :class:`knowledge.multimodal.embedder.DoubaoVisionEmbedder`.
    # Empty ``doubao_vision_api_key`` triggers graceful degradation: the
    # embedder returns a zero vector + warning so the rest of the
    # pipeline (storage, retrieval) still works without vision credentials.
    #
    # ``object_store_*`` drives :class:`knowledge.multimodal.storage.S3ObjectStore`.
    # Default endpoint points at the local MinIO service added to
    # ``deploy/docker-compose.yml``; production overrides with the real
    # AWS S3 endpoint (no code change required — boto3 uses ``endpoint_url``).
    # Storage keys MUST be tenant-prefixed at the call site; these
    # settings only configure the connection, not the layout.
    doubao_vision_api_key: str = Field(default="", alias="DOUBAO_VISION_API_KEY")
    doubao_vision_base_url: str = Field(
        default="https://ark.cn-beijing.volces.com/api/v3",
        alias="DOUBAO_VISION_BASE_URL",
    )
    doubao_vision_model: str = Field(
        default="doubao-embedding-vision",
        alias="DOUBAO_VISION_MODEL",
    )

    object_store_endpoint: str = Field(
        default="http://localhost:9000",
        alias="OBJECT_STORE_ENDPOINT",
    )
    object_store_bucket: str = Field(
        default="lumen-kb",
        alias="OBJECT_STORE_BUCKET",
    )
    object_store_access_key: str = Field(
        default="minioadmin",
        alias="OBJECT_STORE_ACCESS_KEY",
    )
    object_store_secret_key: str = Field(
        default="minioadmin",
        alias="OBJECT_STORE_SECRET_KEY",
    )

    # Stage 18 / M2.B — history mining worker (Task 8).
    #
    # ``history_mining_lookback_days`` — sliding window scanned by the
    # Sunday cron. 30 days balances "enough signal" against
    # "embedding API cost per run". Override per deployment via env.
    #
    # ``min_cluster_size`` / ``max_cluster_size`` — HDBSCAN bounds
    # (see :class:`history_mining.clusterer.HdbscanClusterer`).
    # ``min_cluster_size=10`` matches the unit-test baseline; raising
    # it reduces noise drafts but loses single-digit topic clusters.
    history_mining_lookback_days: int = Field(
        default=30, alias="HISTORY_MINING_LOOKBACK_DAYS"
    )
    history_mining_min_cluster_size: int = Field(
        default=10, alias="HISTORY_MINING_MIN_CLUSTER_SIZE"
    )
    history_mining_max_cluster_size: int = Field(
        default=1000, alias="HISTORY_MINING_MAX_CLUSTER_SIZE"
    )
    # Dedicated LLM wiring for the mining worker. Defaults to empty
    # (= fall back to ``qa_judge_*``) so an operator who tunes the QA
    # judge config doesn't accidentally side-effect the mining
    # pipeline. Override per deployment via ``HISTORY_MINING_PROVIDER``
    # / ``HISTORY_MINING_MODEL`` if mining needs its own model choice.
    history_mining_provider: str = Field(
        default="", alias="HISTORY_MINING_PROVIDER"
    )
    history_mining_model: str = Field(
        default="", alias="HISTORY_MINING_MODEL"
    )

    @property
    def widget_allowed_origins_global(self) -> list[str]:
        """Split the raw env value into a clean list of origins."""
        return _parse_csv(self.widget_allowed_origins_global_raw)

    @model_validator(mode="after")
    def _validate_jwt_secret(self) -> Self:
        if len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        if self.environment == Environment.PRODUCTION and self.jwt_secret in _KNOWN_DEV_SECRETS:
            raise ValueError("Refusing to use known dev/test secret in production")
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


def reset_settings() -> None:
    """Clear the cached settings singleton. For test isolation only."""
    global _settings
    _settings = None