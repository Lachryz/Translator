"""Resolve local runtime paths without importing the translation backend."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "translator_config.json"


def resolve_config_path(config_path: str | Path | None = None) -> Path:
    """An explicit config wins over the environment and the project default."""
    value = config_path if config_path is not None else (
        os.environ.get("TRANSLATOR_CONFIG") or DEFAULT_CONFIG
    )
    return Path(value).expanduser().resolve()


def _resolve_path(
    value: Any, base: Path, field: str, *, follow_symlinks: bool = True,
) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"{field} must be a non-empty path")
    path = Path(value).expanduser()
    path = base / path if not path.is_absolute() else path
    return path.resolve() if follow_symlinks else Path(os.path.abspath(path))


@dataclass(frozen=True)
class RuntimePaths:
    config_path: Path
    two_batch_root: Path
    env_file: Path
    python_path: Path


def resolve_runtime_paths(
    config_path: str | Path | None = None,
    *,
    config: Mapping[str, Any] | None = None,
) -> RuntimePaths:
    """Resolve relative paths beside the config, independently of the caller cwd.

    Only file locations are resolved here; credential contents are never read.
    A null or omitted env_file uses the backend's .env file.
    """
    path = resolve_config_path(config_path)
    if config is None:
        config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, Mapping):
        raise ValueError("translator configuration must be a JSON object")
    base = path.parent
    backend = _resolve_path(
        os.environ.get("TRANSLATOR_TWO_BATCH_ROOT")
        or config.get("two_batch_root", "../2batch"),
        base,
        "two_batch_root",
    )
    env_value = os.environ.get("TRANSLATOR_ENV_FILE") or config.get("env_file")
    env_file = (
        _resolve_path(env_value, base, "env_file")
        if env_value is not None else backend / ".env"
    )
    python_value = os.environ.get("TRANSLATOR_PYTHON")
    python_path = (
        _resolve_path(python_value, base, "TRANSLATOR_PYTHON", follow_symlinks=False)
        if python_value else backend / ".venv" / "bin" / "python"
    )
    return RuntimePaths(path, backend, env_file, python_path)
