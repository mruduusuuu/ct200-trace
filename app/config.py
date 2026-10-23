from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/ct200.db"
    store_backend: str = "json"  # json | mongo
    json_store_dir: str = "./data/generations"
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "ct200"

    # Default is a real provider on purpose: silently falling back to a mock would
    # produce plausible-looking but fake test cases. "mock" must be opted into.
    llm_provider: str = "openai_compat"  # openai_compat | mock
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "llama-3.3-70b-versatile"
    llm_json_mode: bool = True
    llm_max_attempts: int = 3
    llm_timeout_s: float = 60.0
    max_source_chars: int = 20000

    match_fuzzy_threshold: float = 0.72


@lru_cache
def get_settings() -> Settings:
    return Settings()
