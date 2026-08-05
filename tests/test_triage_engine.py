"""Unit tests for the triage engine. No network, no mock library.

Pure functions (prompt extraction, serialization, response parsing) are tested
directly; the Anthropic call is tested with a tiny hand-rolled fake client
injected at the boundary, the same dependency-injection style as the other tests.
"""

import json
from datetime import datetime, timezone

import pytest

from msg_triage import triage_engine
from msg_triage.config import Config
from msg_triage.source_adapter import Conversation, Message, Role
from msg_triage.triage_engine import (
    DEFAULT_MODEL,
    TRIAGE_OPERATION,
    TRIAGE_SYSTEM,
    Animale,
    Gruppo,
    Presidio,
    Promessa,
    Ricovero,
    StatoDimissione,
    Temperatura,
    TriageEngine,
    TriageError,
    Urgenza,
    build_output_schema,
    build_triage_engine,
    load_facts_block,
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


# --- Telemetry: token usage of the one LLM call --------------------------------


class FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class UsageSpy:
    """Stands in for the telemetry wrapper and records the usage calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def usage(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _spy_usage(monkeypatch) -> UsageSpy:
    spy = UsageSpy()
    monkeypatch.setattr(triage_engine, "telemetry", spy)
    return spy


def test_triage_records_token_usage(monkeypatch):
    client = _fake_client_returning({"conversazioni": [_entry(1)]})
    client.messages._responses[0].usage = FakeUsage(1200, 340)
    spy = _spy_usage(monkeypatch)
    engine = TriageEngine(client, now=_fixed_now)

    engine.triage([_conv("c1")])

    assert spy.calls == [
        {
            "model": DEFAULT_MODEL,
            "operation": TRIAGE_OPERATION,
            "input_tokens": 1200,
            "output_tokens": 340,
        }
    ]


def test_triage_records_usage_even_when_the_answer_is_refused(monkeypatch):
    """The tokens are spent before we look at stop_reason: the cost is real."""
    refused = FakeMessage([], stop_reason="refusal")
    refused.usage = FakeUsage(900, 12)
    spy = _spy_usage(monkeypatch)
    engine = TriageEngine(FakeClient([refused]), now=_fixed_now)

    with pytest.raises(TriageError):
        engine.triage([_conv("c1")])

    assert spy.calls[0]["input_tokens"] == 900


def test_triage_records_no_usage_without_an_api_call(monkeypatch):
    spy = _spy_usage(monkeypatch)
    engine = TriageEngine(FakeClient([]), now=_fixed_now)

    engine.triage([])  # empty input short-circuits before the API

    assert spy.calls == []


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


def _config(**over) -> Config:
    base = dict(
        callbell_api_key="k",
        anthropic_api_key="a",
        telegram_bot_token="t",
        telegram_allowed_user_id=1,
        supabase_url="u",
        supabase_key="s",
    )
    base.update(over)
    return Config(**base)


def test_build_triage_engine_uses_injected_client():
    client = FakeClient([])

    engine = build_triage_engine(_config(), client=client)

    assert isinstance(engine, TriageEngine)
    assert engine._client is client


def test_build_triage_engine_follows_the_flag():
    assert build_triage_engine(_config(), client=FakeClient([]))._extract_facts is False

    engine = build_triage_engine(_config(enable_proposals=True), client=FakeClient([]))

    assert engine._extract_facts is True


def test_build_triage_engine_explicit_override_wins_over_the_flag():
    """What makes the real A/B two commands instead of two edits to .env."""
    forced_on = build_triage_engine(_config(), client=FakeClient([]), extract_facts=True)
    forced_off = build_triage_engine(
        _config(enable_proposals=True), client=FakeClient([]), extract_facts=False
    )

    assert forced_on._extract_facts is True
    assert forced_off._extract_facts is False


# --- T10: the byte-for-byte guardians ------------------------------------------
#
# These exist because, before them, NOTHING in this suite enforced the invariant the
# whole facts design rests on: with the flag off, the request sent to the model is
# the request of before, byte for byte. The old prompt test only checked a prefix, a
# substring and a suffix — a new section dropped in the wrong half of the Markdown
# would have passed all three while TRIAGE_SYSTEM silently grew.

# Every judgment property, in the exact order the model receives them. The order is
# not cosmetic: insertion order is what the SDK serializes, and it is also prompt
# order — moving `fatti` above `motivo` would change the sequence in which the model
# fills the fields.
_JUDGMENT_PROPERTIES = [
    "ref",
    "gruppo",
    "motivo",
    "urgenza",
    "presidio",
    "temperatura",
    "stato_sintetico",
    "azione_suggerita",
    "promessa_rilevata",
]

# Deliberately NOT including "proprietario": the temperatura description has said
# "Temperatura emotiva del proprietario" since T3, and it is not a facts leak.
_FACTS_WORDS = ("fatti", "ricovero", "dimissione", "animali", "non_menzionato")

_USER_MESSAGE_BEFORE_T10 = (
    "Fai il triage delle conversazioni WhatsApp qui sotto.\n"
    "Ora corrente di riferimento: 2026-07-17 12:00 UTC.\n"
    "Per ogni conversazione restituisci un oggetto che usa il suo numero [n] come "
    "campo `ref`. Non inventare né riecheggiare identificativi: basta il numero.\n"
    "\n"
    "## Conversazioni\n"
    "[1] Maria Bianchi — canale: whatsapp — presidio: non assegnata\n"
    "  [2026-07-17 12:00 UTC] CLIENTE: il coniglio non mangia"
)


def _item_schema(*, include_facts: bool) -> dict:
    schema = build_output_schema(include_facts=include_facts)
    return schema["properties"]["conversazioni"]["items"]


def _all_strings(node) -> list[str]:
    """Every string anywhere in a schema: keys, enum values and descriptions."""
    if isinstance(node, dict):
        return [s for key, value in node.items() for s in [key, *_all_strings(value)]]
    if isinstance(node, list):
        return [s for value in node for s in _all_strings(value)]
    return [node] if isinstance(node, str) else []


def test_user_message_is_byte_identical_when_facts_are_off():
    """Full-string equality, not a couple of `in` checks.

    The natural way to break `_build_user_message` is a conditional
    `parts.append("")`: the join turns it into one extra newline, which no substring
    assertion can see.
    """
    engine = TriageEngine(FakeClient([]), now=_fixed_now)
    transcript, _ = serialize_conversations([_conv()])

    assert engine._build_user_message(transcript, None) == _USER_MESSAGE_BEFORE_T10


def test_output_schema_off_keeps_the_properties_of_before_in_order():
    item = _item_schema(include_facts=False)

    assert list(item["properties"]) == _JUDGMENT_PROPERTIES
    assert item["required"] == _JUDGMENT_PROPERTIES


def test_output_schema_defaults_to_the_off_schema():
    assert build_output_schema() == build_output_schema(include_facts=False)


def test_output_schema_off_never_mentions_facts_even_in_a_description():
    """Three of this project's four prompt regressions name a field description as a
    cause. A word about the facts leaking into the off schema is the same failure."""
    haystack = " ".join(_all_strings(build_output_schema())).lower()

    for word in _FACTS_WORDS:
        assert word not in haystack, f"{word!r} leaked into the flag-off schema"


def test_triage_system_excludes_the_facts_block():
    """The facts section sits AFTER the developer notes on purpose: anywhere earlier
    and load_triage_system() would swallow it into TRIAGE_SYSTEM in silence."""
    assert triage_engine._FACTS_START not in TRIAGE_SYSTEM
    assert triage_engine._FACTS_DATE_PLACEHOLDER not in TRIAGE_SYSTEM
    assert TRIAGE_SYSTEM.endswith(
        "Non aggiungere preamboli né riepiloghi di quello che stai per fare. "
        "Comincia dal contenuto."
    )


# --- T10: the facts block -------------------------------------------------------


def _fatti(**over) -> dict:
    base = dict(
        ricovero="in_corso",
        dimissione={"stato": "fissata", "quando": "2026-07-18"},
        animali=[{"specie": "coniglio", "nome": "Bunny"}],
        proprietario="Maria Bianchi",
    )
    base.update(over)
    return base


def _explode(*args, **kwargs):
    raise TriageError("blocco fatti rotto")


def test_load_facts_block_extracts_its_section():
    block = load_facts_block()

    assert triage_engine._FACTS_DATE_PLACEHOLDER in block
    assert "Note sul blocco fatti" not in block


def test_load_facts_block_raises_when_the_marker_is_missing(tmp_path):
    doc = tmp_path / "prompt.md"
    doc.write_text("## SYSTEM PROMPT (testo da usare)\nciao\n", encoding="utf-8")

    with pytest.raises(TriageError):
        load_facts_block(doc)


def test_facts_off_survives_an_unreadable_facts_block(monkeypatch):
    """A feature that is off must not be able to prevent a start, so the block is
    never even read. (The real guarantee is "not loaded at import"; this is its
    testable stand-in.)"""
    monkeypatch.setattr(triage_engine, "load_facts_block", _explode)
    client = _fake_client_returning({"conversazioni": [_entry(1)]})

    result = TriageEngine(client, now=_fixed_now).triage([_conv()])

    assert len(result.conversations) == 1
    assert result.conversations[0].fatti is None


def test_facts_on_fails_loudly_when_the_block_is_missing(monkeypatch):
    """ENABLE_PROPOSALS is opt-in: falling back to off in silence would make T10
    mysteriously dead. It fails at construction, not mid-triage."""
    monkeypatch.setattr(triage_engine, "load_facts_block", _explode)

    with pytest.raises(TriageError):
        TriageEngine(FakeClient([]), now=_fixed_now, extract_facts=True)


def test_facts_block_comes_last_after_the_transcript():
    """Before the transcript it becomes the lens the model reads every message
    through — the mechanism of the second tuning's regression."""
    engine = TriageEngine(FakeClient([]), now=_fixed_now, extract_facts=True)
    transcript, _ = serialize_conversations([_conv()])

    message = engine._build_user_message(transcript, None)

    assert message.startswith(_USER_MESSAGE_BEFORE_T10)
    assert message.index("## Conversazioni") < message.index("## Fatti di stato")


def test_facts_block_carries_todays_date_in_the_clinic_timezone():
    """23:30 UTC is already the next day in Rome. "Dimissione oggi" is a same-day
    rule, so a date resolved on the wrong clock is a proposal on the wrong day."""
    engine = TriageEngine(
        FakeClient([]),
        now=lambda: datetime(2026, 7, 17, 23, 30, tzinfo=timezone.utc),
        extract_facts=True,
    )
    transcript, _ = serialize_conversations([_conv()])

    message = engine._build_user_message(transcript, None)

    assert "Oggi è 2026-07-18" in message
    assert triage_engine._FACTS_DATE_PLACEHOLDER not in message


def test_output_schema_on_appends_fatti_last():
    item = _item_schema(include_facts=True)

    assert list(item["properties"]) == [*_JUDGMENT_PROPERTIES, "fatti"]
    assert item["required"] == [*_JUDGMENT_PROPERTIES, "fatti"]


def test_facts_schema_is_strict_all_the_way_down():
    fatti = _item_schema(include_facts=True)["properties"]["fatti"]
    dimissione, null_branch = fatti["properties"]["dimissione"]["anyOf"]

    assert fatti["additionalProperties"] is False
    assert fatti["required"] == ["ricovero", "dimissione", "animali", "proprietario"]
    assert null_branch == {"type": "null"}
    assert dimissione["additionalProperties"] is False
    assert dimissione["properties"]["stato"]["enum"] == ["fissata", "avvenuta"]
    assert fatti["properties"]["animali"]["items"]["additionalProperties"] is False


def test_facts_schema_descriptions_avoid_judgment_vocabulary():
    """The facts must not argue. A description that pulls one way inside the same
    request as a prompt that pulls the other is how the judgment drifts."""
    fatti = _item_schema(include_facts=True)["properties"]["fatti"]
    haystack = " ".join(_all_strings(fatti)).lower()

    for word in ("urgente", "grave", "subito", "priorità", "preoccupat"):
        assert word not in haystack, f"{word!r} is judgment vocabulary"


def test_triage_sends_the_facts_schema_and_the_same_system_prompt():
    client = _fake_client_returning({"conversazioni": [_entry(1, fatti=_fatti())]})
    engine = TriageEngine(client, now=_fixed_now, extract_facts=True)

    engine.triage([_conv()])

    call = client.messages.calls[0]
    item = call["output_config"]["format"]["schema"]["properties"]["conversazioni"]["items"]
    assert "fatti" in item["properties"]
    assert call["system"] == TRIAGE_SYSTEM  # the system prompt never moves


# --- T10: parsing and degradation -----------------------------------------------


def test_parse_builds_fatti():
    data = {"conversazioni": [_entry(1, fatti=_fatti())]}

    fatti = parse_triage_response(data, {1: _conv()}).conversations[0].fatti

    assert fatti.ricovero is Ricovero.IN_CORSO
    assert fatti.dimissione.stato is StatoDimissione.FISSATA
    assert fatti.dimissione.quando == "2026-07-18"
    assert fatti.animali == (Animale(specie="coniglio", nome="Bunny"),)
    assert fatti.proprietario == "Maria Bianchi"


def test_malformed_fatti_cost_their_facts_not_the_entry():
    """The triage is the product, the facts are the side dish: they must never be
    able to make a line disappear from the digest."""
    data = {"conversazioni": [_entry(1, motivo="il coniglio non mangia", fatti={"ricovero": "boh"})]}

    entry = parse_triage_response(data, {1: _conv()}).conversations[0]

    assert entry.fatti is None
    assert entry.motivo == "il coniglio non mangia"
    assert entry.gruppo is Gruppo.IN_CORSO


def test_the_malformed_fatti_warning_never_carries_the_payload(caplog):
    """Facts hold owners' and animals' names, and this line goes to journald."""
    data = {"conversazioni": [_entry(1, fatti={"ricovero": "boh", "proprietario": "Maria Bianchi"})]}

    with caplog.at_level("WARNING"):
        parse_triage_response(data, {1: _conv()})

    assert "Maria Bianchi" not in caplog.text
    assert "ValueError" in caplog.text


def test_quando_is_dropped_when_it_is_not_an_iso_date():
    """PR2 does arithmetic on this field: a half-understood date would mature a
    proposal on the wrong day and nobody would notice. The stato survives."""
    dimissione_raw = {"stato": "fissata", "quando": "domani"}
    data = {"conversazioni": [_entry(1, fatti=_fatti(dimissione=dimissione_raw))]}

    dimissione = parse_triage_response(data, {1: _conv()}).conversations[0].fatti.dimissione

    assert dimissione.stato is StatoDimissione.FISSATA
    assert dimissione.quando is None


def test_fatti_without_a_dimissione_or_animals_are_valid():
    data = {"conversazioni": [_entry(1, fatti=_fatti(dimissione=None, animali=[]))]}

    fatti = parse_triage_response(data, {1: _conv()}).conversations[0].fatti

    assert fatti.dimissione is None
    assert fatti.animali == ()


def test_an_entry_without_fatti_keeps_them_none():
    result = parse_triage_response({"conversazioni": [_entry(1)]}, {1: _conv()})

    assert result.conversations[0].fatti is None
