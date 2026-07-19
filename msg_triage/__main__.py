"""Entry point: load config, then run the Telegram bot (long polling).

Runnable as ``python -m msg_triage`` or via the ``msg-triage`` console script.
Boots by loading ``.env`` (if present) and validating secrets, then hands off to
``telegram_bot.run_bot`` which blocks on long polling until interrupted.
"""

from __future__ import annotations

import logging
from pathlib import Path

from msg_triage.config import ConfigError, load_config, present_keys
from msg_triage.logging_setup import setup_logging

logger = logging.getLogger("msg_triage")

# Repo root = parent of this package directory. Used to anchor the .env lookup.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv_if_present() -> None:
    """Load variables from this project's ``.env`` if python-dotenv is installed.

    We load ONLY ``<project root>/.env`` explicitly. The default
    ``load_dotenv()`` walks up the directory tree and would silently pick up an
    unrelated ``.env`` from a parent folder (e.g. the user's home) -- a
    secrets-hygiene footgun. Existing environment variables always take
    precedence (``override`` defaults to False), so systemd/env vars win in
    production. A missing ``.env`` or missing dependency is a no-op.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(dotenv_path=_PROJECT_ROOT / ".env")


def main() -> int:
    """Boot the app: load config, then run the Telegram bot (blocking)."""
    _load_dotenv_if_present()
    setup_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    logger.info("VetTriage — secrets loaded: %s", ", ".join(present_keys()))
    # Imported lazily so the entry point stays import-light (telegram is pulled in
    # only when we actually launch the bot).
    from msg_triage.telegram_bot import run_bot

    run_bot(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
