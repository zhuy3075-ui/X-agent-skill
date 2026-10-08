from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from Scweet.config import ScweetConfig
from scweet_mcp.store import StoreError


ROOT = Path(__file__).resolve().parents[2]


def load_settings(path: str | None = None) -> ScweetConfig:
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        if not isinstance(values, dict) or set(values) - set(ScweetConfig.model_fields):
            raise ValueError("unknown configuration fields")
        # Runtime secrets belong to the process environment, never this file.
        config = ScweetConfig(**values)
    except (OSError, ValueError, ValidationError):
        raise StoreError("CONFIG_ERROR: Read a valid JSON configuration with ScweetConfig field names.") from None
    for name in ("mcp_artifact_dir", "mcp_manifest_db_path", "mcp_credentials_file", "mcp_knowledge_base_dir"):
        if getattr(config, name) is None:
            continue
        value = Path(getattr(config, name)).expanduser()
        setattr(config, name, str(value if value.is_absolute() else ROOT / value))
    return config
