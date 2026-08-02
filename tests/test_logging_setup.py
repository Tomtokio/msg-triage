"""Unit test for the logging configuration.

Only the httpx pin is covered: it is not noise-suppression but secret hygiene, and
without a test the line reads like a cosmetic tweak somebody may drop to "see more".
"""

from __future__ import annotations

import logging

from msg_triage.logging_setup import setup_logging


def test_httpx_logger_is_pinned_to_warning_even_at_debug():
    """The url of ``getUpdates`` carries the bot token: it must never reach journald.

    DEBUG included, on purpose — that is exactly when someone would forget.
    """
    logging.getLogger("httpx").setLevel(logging.NOTSET)

    setup_logging("DEBUG")

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
