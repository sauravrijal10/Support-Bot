"""
app/config.py

Central place for environment-driven settings. Reads required variables
once and fails fast with a clear message if any is missing, instead of
letting a confusing error surface later.
"""

import os
from dataclasses import dataclass
from functools import lru_cache

MIN_JWT_SECRET_LENGTH = 32


@dataclass(frozen=True)
class Settings:
    # HF_TOKEN first, then HF_FALLBACK_TOKENS in order (see app/services/llm.py).
    hf_tokens: tuple[str, ...]
    database_url: str
    jwt_secret: str
    jwt_expire_minutes: int = 60
    knowledge_dir: str = "knowledge"
    # Must be a model whose provider supports tool calling (phi-4 does not).
    # Pinned provider: "auto" may route to one that doesn't advertise tools.
    llm_model: str = "Qwen/Qwen2.5-72B-Instruct"
    llm_provider: str = "deepinfra"


@lru_cache
def get_settings() -> Settings:
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        raise RuntimeError(
            "HF_TOKEN environment variable is required. "
            "Add it to your .env file -- get a free token at "
            "https://huggingface.co/settings/tokens"
        )

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL environment variable is required. "
            "Add it to your .env file, or use docker-compose which sets "
            "it automatically."
        )

    jwt_secret = os.environ.get("JWT_SECRET")
    if not jwt_secret or len(jwt_secret) < MIN_JWT_SECRET_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET environment variable is required and must be at "
            f"least {MIN_JWT_SECRET_LENGTH} characters. Generate one with: "
            f'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )

    fallbacks = [t.strip() for t in os.environ.get("HF_FALLBACK_TOKENS", "").split(",")]
    hf_tokens = tuple(dict.fromkeys([hf_token.strip(), *[t for t in fallbacks if t]]))

    return Settings(
        hf_tokens=hf_tokens,
        database_url=database_url,
        jwt_secret=jwt_secret,
        jwt_expire_minutes=int(os.environ.get("JWT_EXPIRE_MINUTES", "60")),
        llm_model=os.environ.get("LLM_MODEL", "Qwen/Qwen2.5-72B-Instruct"),
        llm_provider=os.environ.get("LLM_PROVIDER", "deepinfra"),
    )
