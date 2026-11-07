from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _read_key_file() -> dict[str, str]:
    """Read the developer supplied local config without ever logging the secret."""
    path = Path(os.getenv("DS_API_FILE", str(Path.home() / "Downloads" / "ds-api.txt")))
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Lumina Draft")
    db_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/lumina.db")
    api_base_url: str = os.getenv("DS_API_BASE_URL", _read_key_file().get("base_url", "https://api.deepseek.com"))
    api_model: str = os.getenv("DS_API_MODEL", _read_key_file().get("model", "deepseek-flash"))
    max_pages: int = 20


settings = Settings()
