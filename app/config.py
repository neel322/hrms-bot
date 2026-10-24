from functools import lru_cache
from typing import Optional
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration for HRMS bot."""

    # LLM Provider Configuration
    groq_api_key: Optional[str] = None
    groq_model: str = "qwen/qwen3.8-27b"
    groq_fallback_model: str = "openai/gpt-oss-120b"
    llm_reasoning_effort: str = "none"
    llm_tool_max_tokens: int = 300
    llm_answer_max_tokens: int = 600
    policy_min_score: float = 0.45

    # Conversation History Configuration
    history_max_messages: int = 0
    history_max_chars: int = 24000

    # Qdrant Vector Database Configuration
    qdrant_url: Optional[str] = None
    qdrant_api_key: Optional[str] = None
    qdrant_local_path: str = "./qdrant_data"

    # Relational Database Configuration (Supabase PostgreSQL or SQLite fallback)
    database_url: Optional[str] = None
    sqlite_db_path: str = "./hrms.db"

    # Embedding Configuration
    embedding_model_name: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    cache_dir: Optional[str] = Field(
        default="./models",
        validation_alias=AliasChoices("FAST_EMBED_CACHE_DIR", "fast_embed_cache_dir", "CACHE_DIR", "cache_dir"),
        description="Local directory where the embedding model weights are stored after one-time download.",
    )

    # Qdrant Collections
    hr_policies_collection: str = "hr_policies"
    user_memories_collection: str = "user_memories"

    # Ingestion Parameters
    chunk_size: int = 500
    chunk_overlap: int = 50

    # Timezone Configuration
    tz_name: str = "Asia/Kolkata"

    # Authentication & Security
    jwt_secret: Optional[str] = None
    demo_mode: bool = False
    demo_password: Optional[str] = None
    host: str = "127.0.0.1"
    allow_demo_on_network: bool = False
    cookie_secure: Optional[bool] = None
    cors_origins: str = "http://localhost:8000,http://127.0.0.1:8000,http://localhost:8001,http://127.0.0.1:8001"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    """Returns a cached singleton instance of Settings."""
    return Settings()
