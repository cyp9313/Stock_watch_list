"""Unified project configuration and .env loader.

Priority (highest to lowest):
  1. Process environment variables already set.
  2. Explicit env file path passed to ``load_project_env()``.
  3. Project-root ``.env`` file.
  4. Hard-coded defaults in individual modules via ``os.environ.get(..., default)``.

Rules:
  * ``.env`` **never** overrides variables already present in ``os.environ``.
  * The project root is derived from this file's location, **not** from the
    current working directory, so the same ``.env`` is loaded regardless of CWD.
  * Required variables raise ``ConfigError`` when missing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

# ── Project root (this file lives at the project root) ───────────────
PROJECT_ROOT: Path = Path(__file__).resolve().parent


class ConfigError(RuntimeError):
    """Raised when a required configuration value is missing or invalid."""


def _parse_env_line(line: str) -> Optional[tuple[str, str]]:
    """Parse a single ``KEY=value`` line from a .env file.

    Returns ``None`` for blank lines and comments.
    Strips surrounding quotes (``"`` and ``'``) from values.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    # Remove optional surrounding quotes
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    if not key:
        return None
    return key, value


def _load_env_file(env_path: Path, *, override: bool = False) -> int:
    """Load variables from *env_path* into ``os.environ``.

    By default, does **not** override variables already present.
    Returns the number of variables loaded.
    """
    if not env_path.is_file():
        return 0
    count = 0
    try:
        text = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    for raw_line in text.splitlines():
        parsed = _parse_env_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        if override or key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def load_project_env(
    explicit_env_path: Optional[Union[str, Path]] = None,
    *,
    override: bool = False,
) -> Path:
    """Load the project ``.env`` file into ``os.environ``.

    Parameters
    ----------
    explicit_env_path:
        If provided, load this file instead of the default project-root ``.env``.
    override:
        If ``True``, override variables already present in ``os.environ``.

    Returns
    -------
    Path
        The resolved path of the ``.env`` file that was loaded (may not exist).

    Priority (highest first):
      1. Existing ``os.environ`` entries (unless *override*).
      2. The *explicit_env_path* file (if provided and exists).
      3. ``PROJECT_ROOT / ".env"`` (if no explicit path given).
    """
    if explicit_env_path is not None:
        env_path = Path(explicit_env_path).resolve()
    else:
        env_path = PROJECT_ROOT / ".env"
    _load_env_file(env_path, override=override)
    return env_path


def get_config(
    name: str,
    default: Optional[str] = None,
    *,
    required: bool = False,
) -> str:
    """Get a configuration value from ``os.environ``.

    Raises ``ConfigError`` if *required* is ``True`` and the value is missing
    or still equals a ``your_`` placeholder.
    """
    value = os.environ.get(name, default)
    if required:
        if not value or not str(value).strip():
            raise ConfigError(f"Required environment variable '{name}' is not set.")
        if str(value).strip().lower().startswith("your_"):
            raise ConfigError(
                f"Required environment variable '{name}' still has a placeholder value."
            )
    return value  # type: ignore[return-value]


def get_config_int(
    name: str,
    default: int,
    *,
    min_value: Optional[int] = None,
) -> int:
    """Get an integer configuration value with bounds checking."""
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (ValueError, TypeError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    return value


def get_config_float(
    name: str,
    default: float,
    *,
    min_value: Optional[float] = None,
) -> float:
    """Get a float configuration value with bounds checking."""
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except (ValueError, TypeError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    return value


def get_config_bool(name: str, default: bool = False) -> bool:
    """Get a boolean configuration value.

    Truthy: ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    """
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}
