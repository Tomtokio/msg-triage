"""Entry point: load config, log readiness, exit.

Runnable as ``python -m msg_triage`` or via the ``msg-triage`` console script.
In T1 this only verifies that secrets are present and logs "ready"; in T8 it
becomes the Telegram bot launcher.
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
    """Boot the app far enough to confirm the environment is usable."""
    _load_dotenv_if_present()
    setup_logging()
    try:
        load_config()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    logger.info("VetTriage ready — secrets loaded: %s", ", ".join(present_keys()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
