"""Unit tests for the triage engine. No network, no mock library.

Pure functions (prompt extraction, serialization, response parsing) are tested
directly; the Anthropic call is tested with a tiny hand-rolled fake client
injected at the boundary, the same dependency-injection style as the other tests.
"""

import json
from datetime import datetime, timezone

import pytest

from msg_triage.config import Config
from msg_triage.source_adapter import Conversation, Message, Role
from msg_triage.triage_engine import (
    TRIAGE_SYSTEM,
    Gruppo,
    Presidio,
    Promessa,
    Temperatura,
    TriageEngine,
    TriageError,
    Urgenza,
    build_triage_engine,
    load_triage_system,
    parse_triage_response,
    serialize_conversations,
)

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return NOW


def _conv(contact_id: str = "c1", name: str = "Maria Bianchi", **extra) -> Conversation:
    base = dict(
        contact_id=contact_id,
        name=name,
        channel="whatsapp",
        tags=(),
        assigned_user=None,
        messages=(Message(Role.CLIENTE, "il coniglio non mangia", NOW),),
    )
    base.update(extra)
    return Conversation(**base)


def _entry(ref: int, **over) -> dict:
    """A schema-valid model item (judgment fields only)."""
    base = dict(
        ref=ref,
        gruppo="in_corso",
        motivo="m",
        urgenza="media",
        presidio="presidiata",
        temperatura="media",
        stato_sintetico="s",
        azione_suggerita="a",
        promessa_rilevata=None,
    )
    base.update(over)
    return base


# --- Fakes ---------------------------------------------------------------------


class FakeTextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class FakeThinkingBlock:
    type = "thinking"

    def __init__(self, thinking: str = ""):
        self.thinking = thinking


class FakeMessage:
    def __init__(self, content, stop_reason: str = "end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class FakeMessages:
    """Records every create() call and returns queued responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def _fake_client_returning(payload: dict, *, stop_reason: str = "end_turn") -> FakeClient:
    block = FakeTextBlock(json.dumps(payload))
    return FakeClient([FakeMessage([FakeThinkingBlock(), block], stop_reason=stop_reason)])


# --- System prompt loading -----------------------------------------------------


def test_load_triage_system_extracts_operative_section():
    prompt = load_triage_system()
    assert prompt.startswith("Sei l'assistente di triage")
    assert "giornale di bordo" in prompt
    assert "Note per lo sviluppatore" not in prompt
    assert not prompt.endswith("---")


def test_triage_system_constant_matches_loader():
    assert TRIAGE_SYSTEM == load_triage_system()


# --- Serialization -------------------------------------------------------------


def test_serialize_builds_transcript_and_ref_map():
    convos = [
        _conv(
            "c1",
            name="Maria",
            tags=("urgente",),
            assigned_user="giulia@clinica.it",
            messages=(
                Message(Role.OPERATORE, "rispondo", NOW),
                Message(Role.CLIENTE, "grazie", NOW),
            ),
        ),
        _conv("c2", name="Lucia"),
    ]
    text, ref_map = serialize_conversations(convos)

    assert "[1] Maria" in text
    assert "canale: whatsapp" in text
    assert "assegnata a giulia@clinica.it" in text
    assert "tag: urgente" in text
    assert "OPERATORE: rispondo" in text
    assert "CLIENTE: grazie" in text
    assert "[2] Lucia" in text
    assert "non assegnata" in text
    assert ref_map == {1: convos[0], 2: convos[1]}


def test_serialize_skips_empty_text_messages():
    convos = [
        _conv(
            messages=(
                Message(Role.NOTA_SISTEMA, "   ", NOW),
                Message(Role.CLIENTE, "ciao", NOW),
            )
        )
    ]
    text, _ = serialize_conversations(convos)
    assert "NOTA_SISTEMA" not in text
    assert "CLIENTE: ciao" in text


# --- Engine: the single call and mapping ---------------------------------------


def test_triage_short_circuits_on_empty_input():
    client = FakeClient([])
    engine = TriageEngine(client, now=_fixed_now)

    result = engine.triage([])

    assert result.conversations == ()
    assert client.messages.calls == []


def test_triage_calls_model_once_with_expected_params():
    client = _fake_client_returning({"conversazioni": [_entry(1)]})
    engine = TriageEngine(client, now=_fixed_now)

    engine.triage([_conv("c1")])

    assert len(client.messages.calls) == 1
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-4-8"
    assert call["system"] == TRIAGE_SYSTEM
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "high"
    # the transcript (and the reference "now") reach the user message
    user_content = call["messages"][0]["content"]
    assert "il coniglio non mangia" in user_content
    assert "2026-07-17 12:00 UTC" in user_content


def test_triage_takes_contact_id_and_name_from_source_not_model():
    client = _fake_client_returning({"conversazioni": [_entry(1)]})
    engine = TriageEngine(client, now=_fixed_now)

    result = engine.triage([_conv("c1", name="Maria Bianchi")])

    entry = result.conversations[0]
    assert entry.contact_id == "c1"
    assert entry.nome == "Maria Bianchi"


def test_triage_coerces_enums_and_parses_promessa():
    payload = {
        "conversazioni": [
            _entry(
                1,
                gruppo="subito",
                urgenza="emergenza",
                presidio="scoperta",
                temperatura="alta",
                promessa_rilevata={
                    "testo": "le confermo entro due ore",
                    "scadenza_stimata": "2026-07-17 14:00",
                },
            )
        ]
    }
    engine = TriageEngine(_fake_client_returning(payload), now=_fixed_now)

    entry = engine.triage([_conv("c1")]).conversations[0]

    assert entry.gruppo is Gruppo.SUBITO
    assert entry.urgenza is Urgenza.EMERGENZA
    assert entry.presidio is Presidio.SCOPERTA
    assert entry.temperatura is Temperatura.ALTA
    assert entry.promessa_rilevata == Promessa("le confermo entro due ore", "2026-07-17 14:00")


def test_triage_injects_previous_state_when_provided():
    client = _fake_client_returning({"conversazioni": [_entry(1)]})
    engine = TriageEngine(client, now=_fixed_now)

    engine.triage([_conv("c1")], previous_state="c1 era già scoperta stamattina")

    user_content = client.messages.calls[0]["messages"][0]["content"]
    assert "Stato del run precedente" in user_content
    assert "c1 era già scoperta stamattina" in user_content


# --- Engine: error handling ----------------------------------------------------


def test_triage_raises_on_refusal():
    client = FakeClient([FakeMessage([], stop_reason="refusal")])
    engine = TriageEngine(client, now=_fixed_now)

    with pytest.raises(TriageError, match="refus"):
        engine.triage([_conv("c1")])


def test_triage_raises_on_truncation():
    client = FakeClient([FakeMessage([FakeTextBlock("{")], stop_reason="max_tokens")])
    engine = TriageEngine(client, now=_fixed_now)

    with pytest.raises(TriageError, match="truncat"):
        engine.triage([_conv("c1")])


def test_triage_raises_on_invalid_json():
    client = FakeClient([FakeMessage([FakeTextBlock("not json")])])
    engine = TriageEngine(client, now=_fixed_now)

    with pytest.raises(TriageError, match="valid JSON"):
        engine.triage([_conv("c1")])


# --- Response parsing: completeness and robustness -----------------------------


def test_parse_drops_unknown_ref_but_keeps_valid():
    ref_map = {1: _conv("c1")}
    data = {"conversazioni": [_entry(1), _entry(99)]}

    result = parse_triage_response(data, ref_map)

    assert [e.contact_id for e in result.conversations] == ["c1"]


def test_parse_drops_duplicate_ref():
    ref_map = {1: _conv("c1")}
    data = {"conversazioni": [_entry(1, motivo="first"), _entry(1, motivo="second")]}

    result = parse_triage_response(data, ref_map)

    assert len(result.conversations) == 1
    assert result.conversations[0].motivo == "first"


def test_parse_raises_when_no_usable_entries():
    ref_map = {1: _conv("c1")}
    data = {"conversazioni": [_entry(99)]}

    with pytest.raises(TriageError):
        parse_triage_response(data, ref_map)


def test_parse_raises_when_conversazioni_missing():
    with pytest.raises(TriageError, match="conversazioni"):
        parse_triage_response({}, {1: _conv("c1")})


# --- Factory -------------------------------------------------------------------


def test_build_triage_engine_uses_injected_client():
    config = Config(
        callbell_api_key="k",
        anthropic_api_key="a",
        telegram_bot_token="t",
        telegram_allowed_user_id=1,
        supabase_url="u",
        supabase_key="s",
    )
    client = FakeClient([])

    engine = build_triage_engine(config, client=client)

    assert isinstance(engine, TriageEngine)
    assert engine._client is client
