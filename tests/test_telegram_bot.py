"""Unit tests for the Telegram bot (T8). No network, no mock library.

The pure core (``parse_window_hours``, ``split_message``, ``run_triage_pipeline``)
is tested directly with hand-rolled fakes injected at the boundary — the same
dependency-injection style as the other tests. The async handlers are exercised via
``asyncio.run`` with tiny fake Update/Context objects; ``monkeypatch`` is used only
to swap the blocking pipeline in the two paths that actually cross it (so no real
Callbell/Anthropic call happens).
"""

from __future__ import annotations

import asyncio

import pytest

from msg_triage import telegram_bot
from msg_triage.config import Config, load_config
from msg_triage.renderers import render_all
from msg_triage.telegram_bot import (
    DEFAULT_WINDOW_HOURS,
    parse_window_hours,
    run_triage_pipeline,
    split_message,
)
from msg_triage.triage_engine import (
    ConversationTriage,
    Gruppo,
    Presidio,
    Temperatura,
    TriageError,
    TriageResult,
    Urgenza,
)

_COMPLETE_ENV = {
    "CALLBELL_API_KEY": "cb-key",
    "ANTHROPIC_API_KEY": "an-key",
    "TELEGRAM_BOT_TOKEN": "123456:ABC-fake-token",
    "TELEGRAM_ALLOWED_USER_ID": "123456789",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_KEY": "sb-key",
}


def _config() -> Config:
    return load_config(dict(_COMPLETE_ENV))


def _triage_entry(**over) -> ConversationTriage:
    base = dict(
        contact_id="demo-rossi",
        nome="Sig.ra Rossi",
        gruppo=Gruppo.SUBITO,
        motivo="Coniglio non mangia da 24h",
        urgenza=Urgenza.ALTA,
        presidio=Presidio.SCOPERTA,
        temperatura=Temperatura.ALTA,
        stato_sintetico="La sig.ra Rossi segnala un coniglio che non mangia da ieri sera.",
        azione_suggerita="Richiamare per un triage clinico.",
        promessa_rilevata=None,
    )
    base.update(over)
    return ConversationTriage(**base)


def _result(*entries: ConversationTriage) -> TriageResult:
    return TriageResult(conversations=tuple(entries))


# --- Boundary fakes ------------------------------------------------------------


class _FakeAdapter:
    def __init__(self, conversations):
        self._conversations = conversations
        self.calls: list[float] = []

    def fetch_recent_conversations(self, window_hours: float = 6.0):
        self.calls.append(window_hours)
        return self._conversations


class _FakeEngine:
    def __init__(self, result: TriageResult):
        self._result = result
        self.calls: list[tuple] = []

    def triage(self, conversations, *, previous_state=None):
        self.calls.append((conversations, previous_state))
        return self._result


class _FakeMessage:
    def __init__(self):
        self.replies: list[str] = []
        self.parse_modes: list[str | None] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        self.parse_modes.append(kwargs.get("parse_mode"))


class _FakeUpdate:
    def __init__(self, message):
        self.effective_message = message


class _FakeContext:
    def __init__(self, *, config, lock, args):
        self.bot_data = {"config": config, "triage_lock": lock}
        self.args = args


# --- parse_window_hours --------------------------------------------------------


def test_parse_window_hours_defaults_when_absent():
    assert parse_window_hours(None) == DEFAULT_WINDOW_HOURS
    assert parse_window_hours("") == DEFAULT_WINDOW_HOURS
    assert parse_window_hours("   ") == DEFAULT_WINDOW_HOURS


def test_parse_window_hours_valid_values():
    assert parse_window_hours("12") == 12.0
    assert parse_window_hours("6.5") == 6.5
    assert parse_window_hours("6,5") == 6.5  # Italian decimal comma


def test_parse_window_hours_rejects_non_numeric():
    with pytest.raises(ValueError, match="abc"):
        parse_window_hours("abc")


@pytest.mark.parametrize("bad", ["0", "-1", "999", "inf", "nan"])
def test_parse_window_hours_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        parse_window_hours(bad)


# --- split_message -------------------------------------------------------------


def test_split_message_short_is_single_chunk():
    assert split_message("ciao") == ["ciao"]


def test_split_message_empty_yields_one_empty_chunk():
    assert split_message("") == [""]


def test_split_message_exact_boundary_is_single_chunk():
    text = "a" * 100
    assert split_message(text, limit=100) == [text]


def test_split_message_breaks_on_line_boundaries():
    text = "\n".join(f"riga numero {i}" for i in range(100))
    chunks = split_message(text, limit=50)
    assert len(chunks) > 1
    assert all(len(chunk) <= 50 for chunk in chunks)
    # No line is broken across chunks: boundaries fall on the original newlines.
    assert "\n".join(chunks) == text


def test_split_message_hard_splits_an_overlong_line():
    text = "x" * 250
    chunks = split_message(text, limit=100)
    assert chunks == ["x" * 100, "x" * 100, "x" * 50]
    assert "".join(chunks) == text


def test_split_message_never_cuts_an_html_tag():
    # A single overlong line with a tag straddling the limit boundary (HTML-safe path).
    line = "a" * 98 + "<b>x</b>" + "b" * 200
    chunks = split_message(line, limit=100)
    for chunk in chunks:
        lt = chunk.rfind("<")
        assert lt == -1 or ">" in chunk[lt:]  # no chunk ends inside an unclosed tag
    assert "".join(chunks) == line  # hard-split stays lossless


# --- run_triage_pipeline -------------------------------------------------------


def test_run_triage_pipeline_fetches_triages_renders():
    result = _result(_triage_entry())
    adapter = _FakeAdapter(conversations=["conv"])  # engine is faked; content ignored
    engine = _FakeEngine(result)

    got_result, rendered = run_triage_pipeline(_config(), 12.0, adapter=adapter, engine=engine)

    assert got_result is result
    assert rendered == render_all(result)
    assert adapter.calls == [12.0]  # window forwarded to the adapter
    # SEAM T4: memory not wired — triage is called without previous_state.
    assert engine.calls == [(["conv"], None)]


def test_run_triage_pipeline_empty_window():
    empty = _result()
    adapter = _FakeAdapter(conversations=[])
    engine = _FakeEngine(empty)

    result, rendered = run_triage_pipeline(_config(), 6.0, adapter=adapter, engine=engine)

    assert result.conversations == ()
    assert rendered.schema_text  # renderers return the Italian empty-state string


# --- delivery ------------------------------------------------------------------


def test_deliver_triage_sends_three_distinct_messages():
    message = _FakeMessage()
    rendered = render_all(_result(_triage_entry()))

    asyncio.run(telegram_bot._deliver_triage(message, rendered))

    assert len(message.replies) == 3
    assert "SCHEMA" in message.replies[0]
    assert "TABELLA" in message.replies[1]
    assert "VOCALE" in message.replies[2]
    # Schema and table go out as HTML; the voice stays plain text (no parse_mode).
    assert message.parse_modes == ["HTML", "HTML", None]


# --- triage_command paths ------------------------------------------------------


def test_triage_command_rejects_bad_argument():
    message = _FakeMessage()
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=["abc"])

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    # Fails validation before any pipeline work: exactly one reply, naming the value.
    assert len(message.replies) == 1
    assert "abc" in message.replies[0]


def test_triage_command_rejects_overlapping_run():
    message = _FakeMessage()
    lock = asyncio.Lock()

    async def scenario():
        await lock.acquire()  # a run is already in progress
        context = _FakeContext(config=_config(), lock=lock, args=None)
        await telegram_bot.triage_command(_FakeUpdate(message), context)

    asyncio.run(scenario())

    assert len(message.replies) == 1
    assert "già in corso" in message.replies[0]


def test_triage_command_handles_empty_window(monkeypatch):
    empty = _result()
    monkeypatch.setattr(
        telegram_bot, "run_triage_pipeline", lambda config, hours: (empty, render_all(empty))
    )
    message = _FakeMessage()
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=None)

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    # Status message + one "nessuna conversazione" line; no format messages.
    assert len(message.replies) == 2
    assert "Nessuna conversazione" in message.replies[1]


def test_triage_command_delivers_three_formats(monkeypatch):
    result = _result(_triage_entry())
    rendered = render_all(result)
    monkeypatch.setattr(
        telegram_bot, "run_triage_pipeline", lambda config, hours: (result, rendered)
    )
    message = _FakeMessage()
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=["12"])

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    # Status + schema + table + voice.
    assert len(message.replies) == 4
    assert message.replies[0].startswith("🔍")
    assert "SCHEMA" in message.replies[1]
    assert "TABELLA" in message.replies[2]
    assert "VOCALE" in message.replies[3]


def test_triage_command_reports_pipeline_error(monkeypatch):
    def boom(config, hours):
        raise TriageError("il modello ha rifiutato la richiesta")

    monkeypatch.setattr(telegram_bot, "run_triage_pipeline", boom)
    message = _FakeMessage()
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=None)

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    assert any("Errore durante il triage" in reply for reply in message.replies)
    assert any("ha rifiutato" in reply for reply in message.replies)


# --- build_bot (whitelist wiring) ----------------------------------------------


def test_build_bot_wires_config_lock_and_whitelisted_triage():
    config = _config()
    app = telegram_bot.build_bot(config)

    assert app.bot_data["config"] is config
    assert isinstance(app.bot_data["triage_lock"], asyncio.Lock)

    handlers = app.handlers[0]
    triage = next(h for h in handlers if "triage" in getattr(h, "commands", set()))
    # The whitelist lives on the handler filter (only the allowed user reaches it).
    assert triage.filters is not None
