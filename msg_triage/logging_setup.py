"""Base logging configuration.

Developer-facing logs are in English (project convention); user-facing strings
elsewhere are Italian. The log level comes from ``LOG_LEVEL`` (default INFO) and
is not a secret, so it lives here rather than in :class:`~msg_triage.config.Config`.
"""

from __future__ import annotations

import logging
import os
import sys

_DEFAULT_LEVEL = "INFO"
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str | None = None) -> None:
    """Configure the root logger with a console handler on stdout.

    Level resolution: explicit ``level`` argument, then the ``LOG_LEVEL`` env
    var, then INFO. An unknown level name falls back to INFO. The ``httpx`` logger
    is pinned to WARNING regardless of the resolved level — see below.
    """
    resolved = (level or os.environ.get("LOG_LEVEL") or _DEFAULT_LEVEL).upper()
    numeric_level = getattr(logging, resolved, None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    logging.basicConfig(
        level=numeric_level,
        format=_LOG_FORMAT,
        stream=sys.stdout,
    )
    # httpx logs every request at INFO with the FULL url, and Telegram's is
    # api.telegram.org/bot<TOKEN>/getUpdates: the bot token, in clear, every ~10s of
    # long polling. Pinned unconditionally, LOG_LEVEL=DEBUG included — the escape hatch
    # would be used exactly while debugging, which is when nobody is thinking about
    # what ends up in journald. Nothing useful is hidden: Callbell and Supabase go
    # through requests (whose own logging is DEBUG-level, already silent here), and
    # httpx never logs failures anyway — they reach us as exceptions we log ourselves.
    logging.getLogger("httpx").setLevel(logging.WARNING)
