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
from typing import NamedTuple

import pytest

from msg_triage import telegram_bot
from msg_triage.callbell_adapter import CallbellError
from msg_triage.config import Config, load_config
from msg_triage.proposal_executor import ExecutionOutcome
from msg_triage.proposal_store import StoredProposal
from msg_triage.proposals import Proposal, StatoProposta, TipoProposta
from msg_triage.renderers import render_all
from msg_triage.storage import SupabaseError
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
    # Placeholders, as in production today: no test can reach Supabase by accident.
    "SUPABASE_URL": "unused",
    "SUPABASE_KEY": "unused",
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


class _SentMessage(NamedTuple):
    """What Telegram hands back after a send: PR3 needs the id to mark a row delivered."""

    message_id: int


class _FakeMessage:
    def __init__(self):
        self.replies: list[str] = []
        self.parse_modes: list[str | None] = []
        self.markups: list[object] = []
        self._next_id = 1000

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        self.parse_modes.append(kwargs.get("parse_mode"))
        self.markups.append(kwargs.get("reply_markup"))
        self._next_id += 1
        return _SentMessage(self._next_id)


class _FakeQueryMessage:
    """The proposal message a callback query points back at."""

    def __init__(self, text_html: str):
        self.text_html = text_html
        self.text = text_html


class _FakeCallbackQuery:
    def __init__(self, data: str, *, text_html: str = "🏷️ Aggiungere?"):
        self.data = data
        self.message = _FakeQueryMessage(text_html)
        self.answers: list[str | None] = []
        self.edits: list[str] = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class _FakeUpdate:
    def __init__(self, message=None, *, callback_query=None, user_id=None):
        self.effective_message = message
        self.callback_query = callback_query
        self.effective_user = _FakeUser(user_id) if user_id is not None else None


class _FakeUser(NamedTuple):
    id: int


class _FakeContext:
    def __init__(self, *, config, lock=None, args=None):
        self.bot_data = {"config": config, "triage_lock": lock}
        self.args = args


class _FakeErrorContext:
    """Only what ``on_error`` touches: the exception that reached the handler."""

    def __init__(self, error: BaseException):
        self.error = error


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

    got_result, rendered, conversations = run_triage_pipeline(
        _config(), 12.0, adapter=adapter, engine=engine
    )

    assert got_result is result
    assert rendered == render_all(result)
    assert adapter.calls == [12.0]  # window forwarded to the adapter
    # SEAM T4: memory not wired — triage is called without previous_state.
    assert engine.calls == [(["conv"], None)]
    # The neutral conversations come back too: T7 reads last_message_at from them.
    assert conversations == ["conv"]


def test_run_triage_pipeline_empty_window():
    empty = _result()
    adapter = _FakeAdapter(conversations=[])
    engine = _FakeEngine(empty)

    result, rendered, conversations = run_triage_pipeline(
        _config(), 6.0, adapter=adapter, engine=engine
    )

    assert result.conversations == ()
    assert rendered.schema_text  # renderers return the Italian empty-state string
    assert conversations == []


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


def _record_saves(monkeypatch, message) -> list[dict]:
    """Swap the T7 save for a recorder that also snapshots how much was delivered."""
    saves: list[dict] = []

    def fake_save(config, result, rendered, conversations, *, window_hours):
        saves.append(
            {
                "result": result,
                "rendered": rendered,
                "conversations": conversations,
                "window_hours": window_hours,
                "replies_so_far": len(message.replies),
            }
        )
        return True

    monkeypatch.setattr(telegram_bot, "save_triage_run", fake_save)
    return saves


def test_triage_command_handles_empty_window(monkeypatch):
    empty = _result()
    monkeypatch.setattr(
        telegram_bot,
        "run_triage_pipeline",
        lambda config, hours, *, job_id=None: (empty, render_all(empty), []),
    )
    message = _FakeMessage()
    saves = _record_saves(monkeypatch, message)
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=None)

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    # Status message + one "nessuna conversazione" line; no format messages.
    assert len(message.replies) == 2
    assert "Nessuna conversazione" in message.replies[1]
    assert saves == []  # a triage_runs row implies conversations: nothing to save


def test_triage_command_delivers_three_formats(monkeypatch):
    result = _result(_triage_entry())
    rendered = render_all(result)
    monkeypatch.setattr(
        telegram_bot,
        "run_triage_pipeline",
        lambda config, hours, *, job_id=None: (result, rendered, ["conv"]),
    )
    message = _FakeMessage()
    _record_saves(monkeypatch, message)
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=["12"])

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    # Status + schema + table + voice.
    assert len(message.replies) == 4
    assert message.replies[0].startswith("🔍")
    assert "SCHEMA" in message.replies[1]
    assert "TABELLA" in message.replies[2]
    assert "VOCALE" in message.replies[3]


def test_triage_command_saves_the_run_after_delivering_it(monkeypatch):
    result = _result(_triage_entry())
    rendered = render_all(result)
    monkeypatch.setattr(
        telegram_bot,
        "run_triage_pipeline",
        lambda config, hours, *, job_id=None: (result, rendered, ["conv"]),
    )
    message = _FakeMessage()
    saves = _record_saves(monkeypatch, message)
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=["12"])

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    assert len(saves) == 1
    saved = saves[0]
    assert saved["result"] is result
    assert saved["rendered"] is rendered
    assert saved["conversations"] == ["conv"]  # the neutral source, for last_message_at
    assert saved["window_hours"] == 12.0
    # Persistence is never in the critical path: all four messages were already out.
    assert saved["replies_so_far"] == 4


def test_triage_command_builds_proposals_after_delivery_and_before_the_save(monkeypatch):
    result = _result(_triage_entry())
    rendered = render_all(result)
    monkeypatch.setattr(
        telegram_bot,
        "run_triage_pipeline",
        lambda config, hours, *, job_id=None: (result, rendered, ["conv"]),
    )
    message = _FakeMessage()
    # One shared list, so the ORDER is asserted and not just the calls: PR3 delivers the
    # proposals from this spot, and persistence must stay behind them.
    order: list[str] = []
    built: list[dict] = []

    def fake_build(config, built_result, conversations):
        order.append("proposals")
        built.append({"result": built_result, "replies_so_far": len(message.replies)})
        return []

    def fake_save(config, saved, rendered_out, conversations, *, window_hours):
        order.append("save")
        return True

    monkeypatch.setattr(telegram_bot, "build_and_store_proposals", fake_build)
    monkeypatch.setattr(telegram_bot, "save_triage_run", fake_save)
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=None)

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    assert order == ["proposals", "save"]
    assert built[0]["result"] is result
    # Never in the critical path either: the three formats were already out.
    assert built[0]["replies_so_far"] == 4


def test_a_failing_proposal_step_does_not_undo_the_triage(monkeypatch, caplog):
    result = _result(_triage_entry())
    rendered = render_all(result)
    monkeypatch.setattr(
        telegram_bot,
        "run_triage_pipeline",
        lambda config, hours, *, job_id=None: (result, rendered, ["conv"]),
    )

    def boom(config, result, conversations):
        raise RuntimeError("PostgREST è giù")

    monkeypatch.setattr(telegram_bot, "build_and_store_proposals", boom)
    message = _FakeMessage()
    saves = _record_saves(monkeypatch, message)
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=None)

    with caplog.at_level("WARNING", logger="msg_triage.telegram_bot"):
        asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    # The triage is already delivered and the run still saves: a storage problem with
    # the proposals costs the proposals and nothing else. It IS said out loud, because
    # silence would read as "there was nothing to propose". The message never travels.
    assert len(message.replies) == 5
    assert message.replies[-1] == "⚠️ Proposte non disponibili in questo run."
    assert len(saves) == 1
    assert "RuntimeError" in caplog.text
    assert "PostgREST" not in caplog.text


def test_triage_command_reports_pipeline_error(monkeypatch):
    def boom(config, hours, *, job_id=None):
        raise TriageError("il modello ha rifiutato la richiesta")

    monkeypatch.setattr(telegram_bot, "run_triage_pipeline", boom)
    message = _FakeMessage()
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=None)

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    assert any("Errore durante il triage" in reply for reply in message.replies)
    assert any("ha rifiutato" in reply for reply in message.replies)


# --- Telemetry on the run lifecycle ---------------------------------------------


class _Event(NamedTuple):
    """One emission. Severity included: it is the contract with the dashboard (it
    decides the colour of the light), so a spy that drops it would leave the whole
    classification untested."""

    type: str
    severity: str
    metadata: dict


class _TelemetrySpy:
    """Stands in for the telemetry wrapper and records what a run emits, in order."""

    def __init__(self) -> None:
        self.events: list[_Event] = []

    def event(self, type, *, severity="info", message=None, metadata=None) -> None:
        self.events.append(_Event(type, severity, metadata or {}))

    async def aevent(self, type, *, severity="info", message=None, metadata=None) -> None:
        self.event(type, severity=severity, message=message, metadata=metadata)

    def types(self) -> list[str]:
        return [event.type for event in self.events]


def _spy_telemetry(monkeypatch) -> _TelemetrySpy:
    spy = _TelemetrySpy()
    monkeypatch.setattr(telegram_bot, "telemetry", spy)
    return spy


def test_telemetry_pairs_started_with_completed(monkeypatch):
    result = _result(_triage_entry())
    rendered = render_all(result)
    monkeypatch.setattr(
        telegram_bot,
        "run_triage_pipeline",
        lambda config, hours, *, job_id=None: (result, rendered, ["conv"]),
    )
    spy = _spy_telemetry(monkeypatch)
    message = _FakeMessage()
    _record_saves(monkeypatch, message)
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=["12"])

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    assert spy.types() == ["processing_started", "processing_completed"]
    started, completed = spy.events
    assert [event.severity for event in spy.events] == ["info", "info"]
    assert started.metadata["window_hours"] == 12.0
    assert completed.metadata["job_id"] == started.metadata["job_id"]  # one job, one id
    assert completed.metadata["delivered"] is True


def test_telemetry_closes_an_empty_window_as_completed(monkeypatch):
    """Nothing to report is a successful run, not a failure: the pair stays intact."""
    empty = _result()
    monkeypatch.setattr(
        telegram_bot,
        "run_triage_pipeline",
        lambda config, hours, *, job_id=None: (empty, render_all(empty), []),
    )
    spy = _spy_telemetry(monkeypatch)
    message = _FakeMessage()
    _record_saves(monkeypatch, message)
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=None)

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    assert spy.types() == ["processing_started", "processing_completed"]
    assert spy.events[1].severity == "info"  # nothing to report is not an anomaly
    assert spy.events[1].metadata["delivered"] is False
    assert spy.events[1].metadata["n_triaged"] == 0


def test_telemetry_reports_a_failure_without_the_exception_message(monkeypatch):
    secret = "la sig.ra Rossi con il pappagallo"

    def boom(config, hours, *, job_id=None):
        raise TriageError(secret)

    monkeypatch.setattr(telegram_bot, "run_triage_pipeline", boom)
    spy = _spy_telemetry(monkeypatch)
    message = _FakeMessage()
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=None)

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    assert spy.types() == ["processing_started", "processing_failed"]
    failed = spy.events[1]
    # The run failed and the digest never arrived: nothing healed here, so it is the
    # one outcome that must ask for attention. Contrast bot_error, which is a warning.
    assert failed.severity == "error"
    metadata = failed.metadata
    assert metadata["reason"] == "triage_error"
    assert metadata["exception"] == "TriageError"
    # Privacy: only class name and a fixed code travel; the text stays in the log.
    assert secret not in str(metadata)


def test_telemetry_gates_run_before_the_deterministic_checks(monkeypatch):
    """A rejected argument is not a run: no started event to leave dangling."""
    spy = _spy_telemetry(monkeypatch)
    message = _FakeMessage()
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=["-3"])

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    assert spy.events == []


def test_bot_error_is_a_warning_not_an_error(monkeypatch):
    """on_error is also where the long polling drops its failures, and PTB recovers.

    NetworkError towards the Telegram API is the typical case: transient, healed by
    the next retry, and with ``update=None`` (no chat to answer). Classifying it
    `error` would leave a red light in the dashboard for 24h over a problem that no
    longer exists.
    """
    from telegram.error import NetworkError

    spy = _spy_telemetry(monkeypatch)
    context = _FakeErrorContext(NetworkError("Bad Gateway"))

    asyncio.run(telegram_bot.on_error(None, context))

    assert spy.types() == ["bot_error"]
    assert spy.events[0].severity == "warning"
    # Only the class name travels, as everywhere else in this agent.
    assert spy.events[0].metadata == {"exception": "NetworkError"}


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


# --- T10/PR3: consegna delle proposte e tap sui bottoni ------------------------


PROPOSAL_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"
ALLOWED_USER = int(_COMPLETE_ENV["TELEGRAM_ALLOWED_USER_ID"])


def _proposals_config() -> Config:
    return load_config(
        {
            **_COMPLETE_ENV,
            "SUPABASE_URL": "https://demo.supabase.co",
            "SUPABASE_KEY": "eyJh-fake",
            "ENABLE_PROPOSALS": "true",
        }
    )


def _stored(proposal_id=PROPOSAL_ID, tipo=TipoProposta.TAG_ADD, payload=None):
    return StoredProposal(
        id=proposal_id,
        proposal=Proposal(
            contact_id="c1",
            tipo=tipo,
            payload=payload if payload is not None else {"tag": "Ricoverato"},
            motivo="dai messaggi risulta un ricovero in corso",
        ),
    )


def _claimed_row(**over) -> dict:
    """La riga che il claim restituisce a chi l'ha vinta."""
    base = {
        "id": PROPOSAL_ID,
        "contact_id": "c1",
        "tipo": "tag_add",
        "payload": {"tag": "Ricoverato"},
        "motivo": "dai messaggi risulta un ricovero in corso",
        "matures_at": None,
    }
    base.update(over)
    return base


class _FakeProposalStore:
    def __init__(self, *, claimed=None):
        self._claimed = claimed
        self.delivered: list[tuple[str, int]] = []
        self.claims: list[tuple] = []
        self.outcomes: list[tuple] = []

    def mark_delivered(self, proposal_id, telegram_message_id):
        self.delivered.append((proposal_id, telegram_message_id))

    def claim(self, proposal_id, *, stato, decided_at):
        self.claims.append((proposal_id, stato))
        return self._claimed

    def mark_outcome(self, proposal_id, *, stato, executed_at=None):
        self.outcomes.append((proposal_id, stato, executed_at))


def _wire_callback(monkeypatch, *, store, executor=None):
    """Swap the two boundaries the callback crosses: the store and the executor."""
    monkeypatch.setattr(telegram_bot, "build_proposal_store", lambda config: store)
    monkeypatch.setattr(telegram_bot, "build_write_client", lambda config: "write-client")
    if executor is not None:
        monkeypatch.setattr(telegram_bot, "execute", executor)


# --- callback_data (puro) ------------------------------------------------------


def test_callback_data_round_trips():
    data = telegram_bot.build_callback_data(telegram_bot.CALLBACK_OK, PROPOSAL_ID)
    assert telegram_bot.parse_callback_data(data) == (telegram_bot.CALLBACK_OK, PROPOSAL_ID)
    # Il limite di Telegram è 64 byte: un uuid col prefisso ci sta comodo.
    assert len(data.encode()) <= 64


@pytest.mark.parametrize(
    "data",
    [None, "", "t10:ok", "altro:ok:" + PROPOSAL_ID, "t10:forse:" + PROPOSAL_ID, "t10:ok:"],
)
def test_un_payload_che_non_e_nostro_non_si_interpreta(data):
    # L'alternativa sarebbe agire su un id di proposta indovinato.
    assert telegram_bot.parse_callback_data(data) is None


# --- consegna ------------------------------------------------------------------


def test_deliver_proposals_manda_un_messaggio_per_proposta_con_i_bottoni(monkeypatch):
    store = _FakeProposalStore()
    monkeypatch.setattr(telegram_bot, "build_proposal_store", lambda config: store)
    monkeypatch.setattr(telegram_bot, "build_read_client", lambda config: "read-client")
    monkeypatch.setattr(
        telegram_bot,
        "load_deliverable_with_names",
        lambda s, c, *, now: [(_stored(), "Bonifazi"), (_stored(OTHER_ID), "Rossi")],
    )
    message = _FakeMessage()

    sent = asyncio.run(telegram_bot.deliver_proposals(message, _proposals_config()))

    assert sent == 2
    assert len(message.replies) == 2  # mai un blocco unico
    assert "Bonifazi" in message.replies[0]
    assert message.parse_modes == ["HTML", "HTML"]
    assert all(markup is not None for markup in message.markups)
    # Prima si manda, poi si registra: è quello che impedisce una seconda consegna.
    assert store.delivered == [(PROPOSAL_ID, 1001), (OTHER_ID, 1002)]


def test_col_flag_spento_non_si_consegna_niente(monkeypatch):
    monkeypatch.setattr(
        telegram_bot,
        "load_deliverable_with_names",
        lambda *a, **k: pytest.fail("non deve nemmeno guardare la coda"),
    )
    message = _FakeMessage()

    assert asyncio.run(telegram_bot.deliver_proposals(message, _config())) == 0
    assert message.replies == []


# --- tap ✅ / ❌ ---------------------------------------------------------------


def _tap(action, *, store, executor=None, monkeypatch, user_id=ALLOWED_USER):
    _wire_callback(monkeypatch, store=store, executor=executor)
    query = _FakeCallbackQuery(telegram_bot.build_callback_data(action, PROPOSAL_ID))
    update = _FakeUpdate(callback_query=query, user_id=user_id)
    asyncio.run(
        telegram_bot.proposal_callback(update, _FakeContext(config=_proposals_config()))
    )
    return query


def test_un_tap_su_applica_esegue_e_racconta_l_esito(monkeypatch):
    store = _FakeProposalStore(claimed=_claimed_row())
    calls = []

    def fake_execute(stored, *, client, store, now):
        calls.append(stored.id)
        return ExecutionOutcome(True, "Tag «Ricoverato» aggiunto.")

    query = _tap(
        telegram_bot.CALLBACK_OK, store=store, executor=fake_execute, monkeypatch=monkeypatch
    )

    assert calls == [PROPOSAL_ID]
    assert store.claims == [(PROPOSAL_ID, StatoProposta.APPROVATA)]
    assert store.outcomes[0][1] is StatoProposta.ESEGUITA
    assert store.outcomes[0][2] is not None  # executed_at valorizzato
    assert query.edits[0].endswith("⏳ Applico…")
    assert "✅ Tag «Ricoverato» aggiunto." in query.edits[-1]
    # La domanda resta sopra la risposta: la chat racconta cosa è stato chiesto.
    assert query.edits[-1].startswith("🏷️ Aggiungere?")


def test_un_tap_su_ignora_non_tocca_callbell(monkeypatch):
    store = _FakeProposalStore(claimed=_claimed_row())

    def never(*a, **k):
        pytest.fail("❌ non deve eseguire niente")

    query = _tap(telegram_bot.CALLBACK_NO, store=store, executor=never, monkeypatch=monkeypatch)

    assert store.claims == [(PROPOSAL_ID, StatoProposta.RIFIUTATA)]
    assert store.outcomes == []
    assert query.edits[-1].endswith("❌ Ignorata.")


def test_il_secondo_tap_trova_la_proposta_gia_gestita(monkeypatch):
    # La difesa dal doppio tap vive nel DB: il claim non trova più la riga pending.
    store = _FakeProposalStore(claimed=None)

    def never(*a, **k):
        pytest.fail("una proposta già decisa non si riesegue")

    query = _tap(telegram_bot.CALLBACK_OK, store=store, executor=never, monkeypatch=monkeypatch)

    assert store.outcomes == []
    assert "già gestita" in query.edits[-1]


def test_una_riga_illeggibile_dopo_il_claim_si_chiude_come_fallita(monkeypatch):
    store = _FakeProposalStore(claimed=_claimed_row(tipo="boh"))

    def never(*a, **k):
        pytest.fail("non c'è niente da eseguire")

    query = _tap(telegram_bot.CALLBACK_OK, store=store, executor=never, monkeypatch=monkeypatch)

    # Lasciata `approvata` resterebbe lì per sempre a sembrare lavoro in corso.
    assert store.outcomes == [(PROPOSAL_ID, StatoProposta.FALLITA, None)]
    assert "illeggibile" in query.edits[-1]


def test_un_errore_su_callbell_diventa_fallita_e_un_messaggio_non_un_crash(monkeypatch):
    store = _FakeProposalStore(
        claimed=_claimed_row(tipo="rename", payload={"nome": "Mario Rossi"})
    )

    def boom(stored, *, client, store, now):
        raise CallbellError("Callbell error 500 on PATCH /contacts/c1")

    query = _tap(telegram_bot.CALLBACK_OK, store=store, executor=boom, monkeypatch=monkeypatch)

    assert store.outcomes == [(PROPOSAL_ID, StatoProposta.FALLITA, None)]
    assert "⚠️" in query.edits[-1] and "Callbell" in query.edits[-1]


def test_un_tap_da_un_utente_non_autorizzato_resta_in_silenzio(monkeypatch):
    store = _FakeProposalStore(claimed=_claimed_row())

    query = _tap(
        telegram_bot.CALLBACK_OK,
        store=store,
        executor=lambda *a, **k: pytest.fail("niente esecuzione"),
        monkeypatch=monkeypatch,
        user_id=ALLOWED_USER + 1,
    )

    # Nemmeno un answer(): rispondere confermerebbe che il bot esiste.
    assert query.answers == []
    assert query.edits == []
    assert store.claims == []


def test_col_flag_spento_il_tap_e_inerte_e_i_bottoni_restano(monkeypatch):
    # Nessun edit: l'edit toglierebbe la tastiera e lascerebbe la riga pending orfana.
    # Così il kill switch si riaccende senza mettere le mani sul DB.
    monkeypatch.setattr(telegram_bot, "build_proposal_store", lambda config: None)
    query = _FakeCallbackQuery(
        telegram_bot.build_callback_data(telegram_bot.CALLBACK_OK, PROPOSAL_ID)
    )
    update = _FakeUpdate(callback_query=query, user_id=ALLOWED_USER)

    asyncio.run(telegram_bot.proposal_callback(update, _FakeContext(config=_config())))

    assert query.answers == ["Proposte disattivate"]
    assert query.edits == []


def test_un_database_irraggiungibile_al_claim_non_lascia_l_utente_appeso(monkeypatch):
    class _Broken(_FakeProposalStore):
        def claim(self, proposal_id, *, stato, decided_at):
            raise SupabaseError("PostgREST è giù")

    query = _tap(
        telegram_bot.CALLBACK_OK,
        store=_Broken(),
        executor=lambda *a, **k: pytest.fail("niente esecuzione"),
        monkeypatch=monkeypatch,
    )

    assert "Database non raggiungibile" in query.edits[-1]


def test_build_bot_registra_il_gestore_dei_bottoni():
    from telegram.ext import CallbackQueryHandler

    application = telegram_bot.build_bot(_proposals_config())

    handlers = application.handlers[0]
    callback_handlers = [h for h in handlers if isinstance(h, CallbackQueryHandler)]
    assert len(callback_handlers) == 1
    assert callback_handlers[0].callback is telegram_bot.proposal_callback


def test_l_aiuto_nomina_i_bottoni_solo_quando_le_proposte_sono_accese():
    for config, expected in ((_config(), False), (_proposals_config(), True)):
        message = _FakeMessage()
        asyncio.run(
            telegram_bot.start_command(_FakeUpdate(message), _FakeContext(config=config))
        )
        assert ("✅" in message.replies[0]) is expected


def test_anche_una_finestra_vuota_consegna_la_coda_in_attesa(monkeypatch):
    """La coda non è di questo run: una rimozione maturata stamattina deve arrivare
    anche in una giornata silenziosa.

    Stessa lista condivisa del test sull'ordine a finestra piena, così si asserisce
    l'ORDINE e non solo le chiamate. Qui il salvataggio non c'è per costruzione (una riga
    `triage_runs` implica delle conversazioni), quindi quello che si pinna è: le proposte
    stanno DOPO il messaggio all'utente, e non c'è nessun save da scavalcare.
    """
    empty = _result()
    monkeypatch.setattr(
        telegram_bot,
        "run_triage_pipeline",
        lambda config, hours, *, job_id=None: (empty, render_all(empty), []),
    )
    message = _FakeMessage()
    order: list[str] = []
    built: list[dict] = []

    def fake_build(config, built_result, conversations):
        order.append("proposals")
        built.append({"result": built_result, "replies_so_far": len(message.replies)})
        return []

    async def fake_deliver(msg, config):
        order.append("deliver")
        return 0

    monkeypatch.setattr(telegram_bot, "build_and_store_proposals", fake_build)
    monkeypatch.setattr(telegram_bot, "deliver_proposals", fake_deliver)
    saves = _record_saves(monkeypatch, message)
    context = _FakeContext(config=_config(), lock=asyncio.Lock(), args=None)

    asyncio.run(telegram_bot.triage_command(_FakeUpdate(message), context))

    assert order == ["proposals", "deliver"]
    assert saves == []  # niente conversazioni, niente riga triage_runs
    # Mai nel percorso critico: il messaggio all'utente era già uscito.
    assert built[0]["replies_so_far"] == 2
    assert message.replies[-1].startswith("✅ Nessuna conversazione")


def test_un_errore_di_database_prima_della_scrittura_lo_dice_chiaramente(monkeypatch):
    store = _FakeProposalStore(claimed=_claimed_row(tipo="tag_remove"))

    def boom(stored, *, client, store, now):
        raise SupabaseError("system_tags: HTTP 503")

    query = _tap(telegram_bot.CALLBACK_OK, store=store, executor=boom, monkeypatch=monkeypatch)

    # L'esecutore ingoia i guasti DOPO una PATCH riuscita: se arriva fin qui, non è
    # stato scritto niente, e dirlo risparmia un giro su Callbell a controllare.
    assert "niente scritto su Callbell" in query.edits[-1]
    assert store.outcomes == [(PROPOSAL_ID, StatoProposta.FALLITA, None)]
