"""Application configuration loaded from environment variables.

Secrets live ONLY in the environment (never hardcoded, never committed).
``load_config`` reads and validates them, failing fast with a clear message
that lists every missing variable -- without ever exposing a secret value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# All required secret env vars, in a stable order (used for messages/diagnostics).
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "CALLBELL_API_KEY",
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "SUPABASE_URL",
    "SUPABASE_KEY",
)

# The one required var that is not a free-form string but a numeric id.
_INT_KEY = "TELEGRAM_ALLOWED_USER_ID"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Config:
    """Validated application secrets.

    Do not log this object as a whole: it holds secret values. Use
    ``present_keys`` for safe diagnostics (names only).
    """

    callbell_api_key: str
    anthropic_api_key: str
    telegram_bot_token: str
    telegram_allowed_user_id: int
    supabase_url: str
    supabase_key: str


def _read(env: dict[str, str], name: str) -> str | None:
    """Return the stripped value of ``name``, or None if absent/blank."""
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def load_config(env: dict[str, str] | None = None) -> Config:
    """Read and validate configuration from the environment.

    Collects every missing (absent or blank) required variable and raises a
    single :class:`ConfigError` listing them all -- the error never contains a
    secret value. ``env`` defaults to ``os.environ`` and is injectable for tests.
    """
    if env is None:
        env = dict(os.environ)

    values = {name: _read(env, name) for name in REQUIRED_ENV_VARS}
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise ConfigError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    try:
        telegram_allowed_user_id = int(values[_INT_KEY])
    except ValueError as exc:
        raise ConfigError(
            f"{_INT_KEY} must be an integer (the numeric Telegram user id)."
        ) from exc

    return Config(
        callbell_api_key=values["CALLBELL_API_KEY"],
        anthropic_api_key=values["ANTHROPIC_API_KEY"],
        telegram_bot_token=values["TELEGRAM_BOT_TOKEN"],
        telegram_allowed_user_id=telegram_allowed_user_id,
        supabase_url=values["SUPABASE_URL"],
        supabase_key=values["SUPABASE_KEY"],
    )


def present_keys(env: dict[str, str] | None = None) -> list[str]:
    """Return the NAMES of required env vars that are present (never values).

    Safe to log: reveals only which secrets were provided, not their content.
    """
    if env is None:
        env = dict(os.environ)
    return [name for name in REQUIRED_ENV_VARS if _read(env, name) is not None]
