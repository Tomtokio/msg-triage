"""Unit tests for T7 persistence. No network, no mock library.

Record building is pure and tested directly; the PostgREST client is exercised with a
hand-rolled fake session injected at the boundary (the same dependency-injection style
as tests/test_callbell_adapter.py). The three behaviours that matter operationally get
their own tests: the right records go out, a failure never propagates, and placeholder
credentials touch nothing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import pytest
import requests

from msg_triage.config import Config, load_config
from msg_triage.renderers import render_all
from msg_triage.source_adapter import Conversation, Message, Role
from msg_triage.storage import (
    SCHEMA,
    SupabaseStore,
    build_run_record,
    build_state_records,
    build_store,
    is_configured,
    save_triage_run,
)
from msg_triage.triage_engine import (
    ConversationTriage,
    Gruppo,
    Presidio,
    Promessa,
    Temperatura,
    TriageResult,
    Urgenza,
    extract_species,
)

LAST_MESSAGE_AT = datetime(2026, 7, 17, 11, 30, tzinfo=timezone.utc)
RUN_ID = "11111111-2222-3333-4444-555555555555"

_BASE_ENV = {
    "CALLBELL_API_KEY": "cb-key",
    "ANTHROPIC_API_KEY": "an-key",
    "TELEGRAM_BOT_TOKEN": "123456:ABC-fake-token",
    "TELEGRAM_ALLOWED_USER_ID": "123456789",
}


def _config(url: str = "https://demo.supabase.co", key: str = "eyJh-fake") -> Config:
    return load_config({**_BASE_ENV, "SUPABASE_URL": url, "SUPABASE_KEY": key})


def _placeholder_config() -> Config:
    """Production as it stands today: placeholders to satisfy the boot fail-fast."""
    return _config(url="unused", key="unused")


def _entry(**over) -> ConversationTriage:
    base = dict(
        contact_id="cb-rossi",
        nome="Sig.ra Rossi",
        gruppo=Gruppo.SUBITO,
        motivo="la **tartaruga** Bianca non mangia da due giorni",
        urgenza=Urgenza.ALTA,
        presidio=Presidio.SCOPERTA,
        temperatura=Temperatura.ALTA,
        stato_sintetico="La signora segnala che la tartaruga non mangia.",
        azione_suggerita="Richiamare per un triage clinico.",
        promessa_rilevata=None,
    )
    base.update(over)
    return ConversationTriage(**base)


def _conversation(contact_id: str = "cb-rossi", *, messages=None) -> Conversation:
    if messages is None:
        messages = (
            Message(role=Role.CLIENTE, text="Buongiorno", timestamp=LAST_MESSAGE_AT),
        )
    return Conversation(
        contact_id=contact_id,
        name="Sig.ra Rossi",
        channel="whatsapp",
        tags=(),
        assigned_user=None,
        messages=tuple(messages),
    )


def _result(*entries: ConversationTriage) -> TriageResult:
    return TriageResult(conversations=tuple(entries))


def _fixed_id() -> uuid.UUID:
    return uuid.UUID(RUN_ID)


# --- Fakes ---------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=201, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeSession:
    """Records every POST and returns queued responses in order."""

    def __init__(self, responses=None, raises=None):
        self._responses = list(responses or [])
        self._raises = raises
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self._raises is not None:
            raise self._raises
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse()


# --- is_configured: the feature flag -------------------------------------------


def test_is_configured_rejects_the_production_placeholder():
    assert is_configured(_placeholder_config()) is False


def test_is_configured_accepts_a_real_project_url():
    assert is_configured(_config()) is True


@pytest.mark.parametrize("url", ["unused", "http://demo.supabase.co", "demo.supabase.co"])
def test_is_configured_rejects_anything_that_is_not_https(url):
    # Blank values never get here: load_config rejects them at boot.
    assert is_configured(_config(url=url)) is False


def test_build_store_returns_none_when_not_configured():
    assert build_store(_placeholder_config()) is None
    assert build_store(_config()) is not None


# --- The placeholder path: nothing must reach the network ----------------------


def test_placeholder_credentials_build_nothing_and_call_nothing(monkeypatch):
    """The gate must fire before an HTTP session even exists, let alone a request.

    Counting constructions rather than raising: ``save_triage_run`` swallows every
    exception by contract, so an exploding fake would be swallowed and prove nothing.
    """
    built: list[int] = []
    monkeypatch.setattr(requests, "Session", lambda: built.append(1))
    result = _result(_entry())

    saved = save_triage_run(
        _placeholder_config(),
        result,
        render_all(result),
        [_conversation()],
        window_hours=6.0,  # no store injected: the real gate has to decide
    )

    assert saved is False
    assert built == []  # no session built, so certainly no request made


# --- Record building (pure) ----------------------------------------------------


def test_build_run_record_carries_the_three_texts_and_the_window():
    result = _result(_entry(), _entry(contact_id="cb-neri"))
    rendered = render_all(result)

    record = build_run_record(rendered, run_id=RUN_ID, window_hours=12.0, n_conversations=2)

    assert record["id"] == RUN_ID
    assert record["window_hours"] == 12.0
    assert record["n_conversations"] == 2
    assert record["schema_text"] == rendered.schema_text
    assert record["table_text"] == rendered.table_text
    assert record["vocal_text"] == rendered.vocal_text
    assert "created_at" not in record  # left to the database default


def test_build_state_records_maps_the_whole_triage_entry():
    entry = _entry()
    records = build_state_records(_result(entry), [_conversation()], run_id=RUN_ID)

    assert len(records) == 1
    row = records[0]
    assert row["run_id"] == RUN_ID
    assert row["contact_id"] == "cb-rossi"
    assert row["nome"] == "Sig.ra Rossi"
    assert row["gruppo"] == "subito"  # enums go out as their string value
    assert row["urgenza"] == "alta"
    assert row["presidio"] == "scoperta"
    assert row["temperatura"] == "alta"
    assert row["motivo"] == entry.motivo
    assert row["stato_sintetico"] == entry.stato_sintetico
    assert row["azione_suggerita"] == entry.azione_suggerita
    assert row["specie"] == "tartaruga"
    assert row["last_message_at"] == LAST_MESSAGE_AT.isoformat()
    assert row["promessa_testo"] is None
    assert row["promessa_scadenza_stimata"] is None


def test_build_state_records_keeps_a_vague_promise_deadline_as_text():
    # The model may answer "entro sera" instead of a date: the column is text on purpose.
    entry = _entry(
        promessa_rilevata=Promessa(testo="le confermo entro due ore", scadenza_stimata="entro sera")
    )
    row = build_state_records(_result(entry), [_conversation()], run_id=RUN_ID)[0]

    assert row["promessa_testo"] == "le confermo entro due ore"
    assert row["promessa_scadenza_stimata"] == "entro sera"


def test_build_state_records_without_messages_leaves_last_message_at_null():
    row = build_state_records(
        _result(_entry()), [_conversation(messages=())], run_id=RUN_ID
    )[0]
    assert row["last_message_at"] is None


def test_build_state_records_without_a_matching_conversation_leaves_last_message_at_null():
    # Cannot happen (contact_id comes from the source), but it must lose one field
    # rather than the whole insert.
    row = build_state_records(_result(_entry()), [], run_id=RUN_ID)[0]
    assert row["last_message_at"] is None
    assert row["contact_id"] == "cb-rossi"


# --- extract_species: NULL is an expected outcome, not a failure ---------------


def test_extract_species_reads_the_marker_from_motivo():
    assert extract_species(_entry()) == "tartaruga"


def test_extract_species_falls_back_to_stato_sintetico_then_azione():
    from_stato = _entry(
        motivo="chiede la ricetta",
        stato_sintetico="Il **pappagallo** del signor Neri ha finito le gocce.",
    )
    assert extract_species(from_stato) == "pappagallo"

    from_azione = _entry(
        motivo="chiede la ricetta",
        stato_sintetico="Chiede la ricetta.",
        azione_suggerita="Confermare la terapia per il **coniglio**.",
    )
    assert extract_species(from_azione) == "coniglio"


def test_extract_species_returns_none_when_the_model_never_marked_it():
    # The common case, and NOT a bug: the model marks the species when it names it,
    # and "la dimissione del coniglio" names it without marking it.
    unmarked = _entry(
        motivo="la dimissione del coniglio è fissata per oggi",
        stato_sintetico="Dimissione confermata per le 16:30.",
        azione_suggerita="",
    )
    assert extract_species(unmarked) is None


def test_extract_species_ignores_an_unbalanced_marker():
    assert extract_species(_entry(motivo="il suo **parrocchetto", stato_sintetico="", azione_suggerita="")) is None


def test_extract_species_ignores_a_whole_clause_marked_by_mistake():
    # Marking a sentence breaks the "marca solo la specie" rule: NULL beats storing prose.
    long_marker = _entry(
        motivo="**la tartaruga Bianca della signora Rossi non mangia da due giorni**",
        stato_sintetico="",
        azione_suggerita="",
    )
    assert extract_species(long_marker) is None


# --- The HTTP call: what actually goes to PostgREST ----------------------------


def _save_with_fake_session(session, *, entries=None, conversations=None, hours=6.0):
    entries = entries if entries is not None else [_entry()]
    conversations = conversations if conversations is not None else [_conversation()]
    result = _result(*entries)
    config = _config()
    store = SupabaseStore(config.supabase_url, config.supabase_key, session=session)
    return save_triage_run(
        config,
        result,
        render_all(result),
        conversations,
        window_hours=hours,
        store=store,
        new_id=_fixed_id,
    )


def test_save_posts_the_run_then_the_states():
    session = FakeSession()

    assert _save_with_fake_session(session) is True

    assert len(session.calls) == 2
    # Order is mandatory: conversation_states.run_id is a foreign key.
    assert session.calls[0]["url"].endswith("/rest/v1/triage_runs")
    assert session.calls[1]["url"].endswith("/rest/v1/conversation_states")
    # Both inserts share the client-generated run id, so neither reads the other's response.
    assert session.calls[0]["json"][0]["id"] == RUN_ID
    assert session.calls[1]["json"][0]["run_id"] == RUN_ID


def test_save_sends_the_custom_schema_headers():
    session = FakeSession()
    _save_with_fake_session(session)

    headers = session.calls[0]["headers"]
    assert headers["Content-Profile"] == SCHEMA  # without this PostgREST looks in public
    assert headers["Prefer"] == "return=minimal"
    assert headers["apikey"] == "eyJh-fake"
    assert headers["Authorization"] == "Bearer eyJh-fake"
    assert session.calls[0]["timeout"] > 0  # never hangs the bot


def test_save_bulk_inserts_all_states_in_one_call():
    entries = [_entry(), _entry(contact_id="cb-neri", nome="Sig. Neri")]
    conversations = [_conversation(), _conversation("cb-neri")]
    session = FakeSession()

    _save_with_fake_session(session, entries=entries, conversations=conversations)

    assert len(session.calls) == 2  # not one call per conversation
    assert len(session.calls[1]["json"]) == 2
    assert session.calls[0]["json"][0]["n_conversations"] == 2


def test_save_skips_the_states_call_when_there_is_nothing_to_write():
    session = FakeSession()
    config = _config()
    store = SupabaseStore(config.supabase_url, config.supabase_key, session=session)
    empty = _result()

    save_triage_run(
        config, empty, render_all(empty), [], window_hours=6.0, store=store, new_id=_fixed_id
    )

    assert len(session.calls) == 1


# --- Failure is a WARNING, never an exception ----------------------------------


def test_network_error_logs_a_warning_and_does_not_raise(caplog):
    session = FakeSession(raises=requests.ConnectionError("connessione rifiutata"))

    with caplog.at_level(logging.WARNING, logger="msg_triage.storage"):
        saved = _save_with_fake_session(session)

    assert saved is False  # no exception escaped: the triage is already delivered
    assert "Supabase save failed" in caplog.text
    assert "ConnectionError" in caplog.text


def test_http_error_logs_status_and_message_but_never_the_offending_row(caplog):
    # PostgREST puts the rejected values in "details"/"hint": those must not reach the journal.
    body = {
        "code": "42501",
        "message": "permission denied for table triage_runs",
        "details": 'Failing row contains (Sig.ra Rossi, la tartaruga Bianca non mangia)',
        "hint": None,
    }
    session = FakeSession(responses=[FakeResponse(status_code=401, payload=body)])

    with caplog.at_level(logging.WARNING, logger="msg_triage.storage"):
        saved = _save_with_fake_session(session)

    assert saved is False
    assert "401" in caplog.text
    assert "42501" in caplog.text
    assert "permission denied" in caplog.text
    assert "Sig.ra Rossi" not in caplog.text
    assert "Failing row" not in caplog.text


def test_non_json_error_body_does_not_break_the_warning(caplog):
    session = FakeSession(responses=[FakeResponse(status_code=502, payload=None)])

    with caplog.at_level(logging.WARNING, logger="msg_triage.storage"):
        assert _save_with_fake_session(session) is False

    assert "502" in caplog.text


def test_failure_on_the_states_call_leaves_no_exception_behind(caplog):
    session = FakeSession(
        responses=[FakeResponse(status_code=201), FakeResponse(status_code=400, payload=None)]
    )

    with caplog.at_level(logging.WARNING, logger="msg_triage.storage"):
        assert _save_with_fake_session(session) is False

    assert len(session.calls) == 2
    assert "conversation_states" in caplog.text
