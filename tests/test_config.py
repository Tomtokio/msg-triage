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


# --- ENABLE_PROPOSALS (T10): optional, and never able to stop a boot ------------


def test_enable_proposals_defaults_to_off_when_absent():
    """The six required vars are the whole boot contract; a feature is not part of
    it. Same reasoning that keeps the TELEMETRY_* vars out of the list."""
    assert load_config(dict(_COMPLETE_ENV)).enable_proposals is False


def test_enable_proposals_treats_blank_as_absent():
    env = dict(_COMPLETE_ENV, ENABLE_PROPOSALS="   ")

    assert load_config(env).enable_proposals is False


@pytest.mark.parametrize("raw", ["true", "TRUE", " True ", "1", "yes", "on"])
def test_enable_proposals_accepts_the_usual_true_spellings(raw):
    assert load_config(dict(_COMPLETE_ENV, ENABLE_PROPOSALS=raw)).enable_proposals is True


@pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", "off"])
def test_enable_proposals_accepts_the_usual_false_spellings(raw):
    assert load_config(dict(_COMPLETE_ENV, ENABLE_PROPOSALS=raw)).enable_proposals is False


def test_enable_proposals_rejects_an_unrecognisable_value():
    """A flag typed wrong is a configuration error. Defaulting it to off in silence
    means discovering the typo as "the feature simply never ran"."""
    env = dict(_COMPLETE_ENV, ENABLE_PROPOSALS="vero")

    with pytest.raises(ConfigError, match="ENABLE_PROPOSALS") as exc_info:
        load_config(env)

    message = str(exc_info.value)
    assert "true" in message and "false" in message  # says what IS accepted
    assert "vero" not in message  # and never echoes what was typed


def test_present_keys_ignores_the_optional_flag():
    """The boot diagnostic covers required vars only, so run_bot() logs the flag's
    state separately -- otherwise the only way to know is to read .env on the VPS."""
    env = dict(_COMPLETE_ENV, ENABLE_PROPOSALS="true")

    assert "ENABLE_PROPOSALS" not in present_keys(env)
