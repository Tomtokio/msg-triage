"""Smoke tests for configuration loading (justify the [dev]/pytest extra)."""

from __future__ import annotations

import pytest

from msg_triage.config import Config, ConfigError, load_config, present_keys

_COMPLETE_ENV = {
    "CALLBELL_API_KEY": "cb-key",
    "ANTHROPIC_API_KEY": "an-key",
    "TELEGRAM_BOT_TOKEN": "tg-token",
    "TELEGRAM_ALLOWED_USER_ID": "123456789",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_KEY": "sb-key",
}


def test_load_config_complete_env_returns_config():
    config = load_config(dict(_COMPLETE_ENV))
    assert isinstance(config, Config)
    assert config.telegram_allowed_user_id == 123456789
    assert isinstance(config.telegram_allowed_user_id, int)
    assert config.callbell_api_key == "cb-key"


def test_load_config_lists_all_missing_vars():
    env = dict(_COMPLETE_ENV)
    del env["ANTHROPIC_API_KEY"]
    del env["SUPABASE_KEY"]
    with pytest.raises(ConfigError) as exc_info:
        load_config(env)
    message = str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "SUPABASE_KEY" in message


def test_load_config_treats_blank_as_missing():
    env = dict(_COMPLETE_ENV)
    env["CALLBELL_API_KEY"] = "   "
    with pytest.raises(ConfigError, match="CALLBELL_API_KEY"):
        load_config(env)


def test_load_config_rejects_non_numeric_user_id():
    env = dict(_COMPLETE_ENV)
    env["TELEGRAM_ALLOWED_USER_ID"] = "not-a-number"
    with pytest.raises(ConfigError, match="TELEGRAM_ALLOWED_USER_ID"):
        load_config(env)


def test_present_keys_reports_names_only():
    env = dict(_COMPLETE_ENV)
    del env["SUPABASE_KEY"]
    present = present_keys(env)
    assert "CALLBELL_API_KEY" in present
    assert "SUPABASE_KEY" not in present
    # Names only -- a secret value must never appear.
    assert "cb-key" not in present
