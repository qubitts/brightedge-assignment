from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/brightedge"

    @model_validator(mode="after")
    def _normalize_database_url(self):
        """Render/Heroku provide postgres:// URLs; SQLAlchemy+asyncpg needs postgresql+asyncpg://."""
        url = self.DATABASE_URL
        if url.startswith("postgres://"):
            self.DATABASE_URL = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            self.DATABASE_URL = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return self

    # Redis
    REDIS_URL: str = "redis://localhost:6379"
    CACHE_TTL_SECONDS: int = 86400  # 24 hours

    # Fetcher
    MAX_CONTENT_SIZE: int = 10485760  # 10MB
    FETCH_TIMEOUT: int = 30
    MAX_RETRIES: int = 3
    MAX_CONCURRENCY: int = 10

    # Logging
    LOG_LEVEL: str = "INFO"

    # Classification
    CLASSIFICATION_MODE: str = "nlp"  # nlp | llm

    # LLM
    LLM_PROVIDER: str = "openai"  # openai | anthropic | gemini
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    LLM_MODEL: str = "gpt-4o-mini"  # Used by openai/anthropic providers
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # Content validation
    MIN_CONTENT_LENGTH: int = 200

    model_config = {"env_file": ".env", "env_prefix": "", "case_sensitive": True}


settings = Settings()
