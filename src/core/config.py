"""Application configuration loaded from environment variables and .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict
from src.core.logger import logger


class Settings(BaseSettings):
    """Centralised settings resolved from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    API_BASE: str
    MODEL_ID: str
    NUM_CTX: int
    # Optional: only required when the HuggingFace backend (/scrape/huggingface)
    # is actually used. Left unset, the app still starts and /scrape/ollama works.
    HF_MODEL: str | None = None
    HF_TOKEN: str | None = None


logger.info("Loading application configuration...")
settings = Settings()  # type: ignore[call-arg]
logger.info("Application configuration loaded successfully")