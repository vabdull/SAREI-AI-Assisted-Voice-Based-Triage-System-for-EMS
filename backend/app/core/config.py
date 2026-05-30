"""Application configuration.

All runtime settings live in a single ``Settings`` object whose values
come from environment variables / the project-root ``.env`` file (see
``.env.example``). Grouped into: database, JWT/auth, CORS, ASR model,
recording storage, and the Ollama/Qwen LLM endpoint.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve the project root from this file's location
# (backend/app/core/config.py -> four parents up = repository root) so
# default paths work regardless of the current working directory.
ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
_DB_PATH = ROOT_DIR / "ems_triage.db"
_DEFAULT_ASR_MODEL_PATH = (
    ROOT_DIR / "models" / "FastConformer-Arabic-SADA-Finetune-baseline-v1_final.nemo"
)


class Settings(BaseSettings):
    """Typed, env-driven settings for the whole backend."""

    app_name: str = "AI-Assisted EMS Triage System"
    debug: bool = False
    database_url: str = f"sqlite:///{_DB_PATH}"
    secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 60
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ]

    nemo_model_path: str = str(_DEFAULT_ASR_MODEL_PATH)
    asr_decoder_type: Literal["rnnt", "ctc"] = "rnnt"
    asr_device: Literal["auto", "cpu", "cuda"] = "auto"
    asr_batch_size: int = 4
    recording_storage_dir: str = "data/recordings"
    stream_temp_dir: str = "data/stream_temp"
    max_recording_size_mb: int = 100
    max_stream_duration_seconds: int = 3600

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:14b"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached with ``lru_cache`` so the ``.env`` file is parsed once and the
    same ``Settings`` instance is reused everywhere it is injected.
    """
    return Settings()
