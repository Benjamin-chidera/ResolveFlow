from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application runtime configuration."""

    APP_ENV: str = "development"
    APP_NAME: str = "ResolveFlow AI"
    PORT: int = 8000

    # SQLite Database configuration
    DATABASE_URL: str = "sqlite:///./data/resolveflow.db"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Ollama LLM Configuration
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.2"

    # Financial & Recovery Guardrails
    HIGH_VALUE_THRESHOLD_CENTS: int = 1_000_000  # $10,000.00
    FRAUD_RISK_THRESHOLD: float = 0.70  # Requiring human operator intervention

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
