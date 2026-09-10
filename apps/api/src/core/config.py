from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.settings import Environment

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

    # Observability
    log_level: str = "INFO"
    service_name: str = "ai-customer-api"

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