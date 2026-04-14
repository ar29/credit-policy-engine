from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    app_name: str = "Prayaan Credit Engine"
    app_env: str = "development"
    log_level: str = "INFO"

    # Database (Postgres)
    database_url: str = Field(
        default="postgresql://temporal:temporal@postgres:5432/temporal",
        validation_alias="DATABASE_URL"
    )
    
    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    
    # Temporal
    temporal_server_url: str = "localhost:7233"
    temporal_task_queue: str = "policy-queue"
    
    # LLM
    ollama_base_url: str = "http://localhost:11434/api/generate"
    ollama_model: str = "deepseek-r1:1.5b"
    openai_api_key: str = "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"
    
    # File Paths
    policy_file_path: str = "data/policy.txt"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

# Instantiate as a singleton to be imported across the app
settings = Settings()