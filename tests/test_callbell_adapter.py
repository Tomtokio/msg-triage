"""Unit tests for the Callbell source adapter. No network, no mock library.

Mapping logic is tested directly on dicts; the HTTP client and the windowed
fetch are tested with tiny hand-rolled fakes injected at the boundary (the same
dependency-injection style as tests/test_config.py).
"""

from datetime import datetime, timezone

import pytest

from msg_triage.callbell_adapter import (
    CallbellClient,
    CallbellError,
    CallbellSourceAdapter,
    _build_conversation,
    _is_system_note,
    _message_role,
    _parse_ts,
    _to_message,
    build_adapter,
)
from msg_triage.config import Config
from msg_triage.source_adapter import Message, Role

# A fixed "now" so the time window is deterministic. Cutoff for a 6h window is
# 2026-07-17T06:00:00Z.
NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return NOW


def _msg(created_at: str, status: str, text: str = "x", **extra) -> dict:
    return {"createdAt": created_at, "status": status, "text": text, **extra}


def _contact(uuid: str, **extra) -> dict:
    base = {
        "uuid": uuid,
        "name": "Contact",
        "tags": [],
        "assignedUser": None,
        "channel": {"type": "whatsapp"},
    }
    base.update(extra)
    return base


# --- Fakes ---------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    """Returns queued responses in order and records every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        return self._responses.pop(0)


class FakeClient:
    """Stands in for CallbellClient in adapter tests, with the same surface."""

    def __init__(self, contacts, messages_by_uuid):
        self._contacts = contacts
        self._messages_by_uuid = messages_by_uuid
        self.request_count = 0
        self.messages_requested = []

    def iter_contacts(self):
        for contact in self._contacts:
            yield contact

    def iter_messages(self, contact_uuid):
        self.request_count += 1
        self.messages_requested.append(contact_uuid)
        yield from self._messages_by_uuid.get(contact_uuid, [])


# --- Pure mapping helpers ------------------------------------------------------


def test_parse_ts_returns_utc_aware_datetime():
    parsed = _parse_ts("2026-07-15T14:05:03Z")
    assert parsed == datetime(2026, 7, 15, 14, 5, 3, tzinfo=timezone.utc)
    assert parsed.tzinfo is not None


def test_message_role_maps_received_and_sent():
    assert _message_role({"status": "received"}) == Role.CLIENTE
    assert _message_role({"status": "sent"}) == Role.OPERATORE


def test_colleague_note_is_nota_interna():
    note = {"status": "note", "uuid": "n1", "from": "agent-a", "to": "agent-b"}
    assert not _is_system_note(note)
    assert _message_role(note) == Role.NOTA_INTERNA


def test_system_note_is_nota_sistema():
    note = {
        "status": "note",
        "text": "Conversation was assigned to Tommaso",
        "from": "x",
        "to": "x",
    }
    assert _is_system_note(note)
    assert _message_role(note) == Role.NOTA_SISTEMA


def test_unknown_status_falls_back_to_nota_sistema():
    assert _message_role({"status": "delivered"}) == Role.NOTA_SISTEMA


def test_to_message_defaults_missing_text_to_empty_string():
    message = _to_message({"createdAt": "2026-07-17T11:00:00Z", "status": "received", "text": None})
    assert message.text == ""
    assert message.role == Role.CLIENTE
    assert message.timestamp == datetime(2026, 7, 17, 11, 0, 0, tzinfo=timezone.utc)


def test_build_conversation_maps_contact_fields():
    contact = {
        "uuid": "u1",
        "name": "Mario",
        "tags": ["urgente", "coniglio"],
        "assignedUser": "",  # empty string must become None
        "channel": {"type": "whatsapp", "title": "WhatsApp"},
    }
    messages = (Message(Role.CLIENTE, "ciao", NOW),)
    convo = _build_conversation(contact, messages)
    assert convo.contact_id == "u1"
    assert convo.name == "Mario"
    assert convo.channel == "whatsapp"
    assert convo.tags == ("urgente", "coniglio")
    assert convo.assigned_user is None
    assert convo.messages == messages


# --- HTTP client: pagination, auth, rate limit, errors -------------------------


def test_client_paginates_until_last_page_and_sends_auth_and_page_params():
    session = FakeSession(
        [
            FakeResponse({"contacts": [{"uuid": "a"}], "meta": {"page": 1, "pages": 2}}),
            FakeResponse({"contacts": [{"uuid": "b"}], "meta": {"page": 2, "pages": 2}}),
        ]
    )
    client = CallbellClient("key", session=session, sleep=lambda s: None, throttle=0)

    contacts = list(client.iter_contacts())

    assert [c["uuid"] for c in contacts] == ["a", "b"]
    assert len(session.calls) == 2
    assert session.calls[0]["headers"] == {"Authorization": "Bearer key"}
    assert session.calls[0]["params"] == {"page": 1}
    assert session.calls[1]["params"] == {"page": 2}
    assert client.request_count == 2


def test_client_retries_after_429_honouring_retry_after():
    session = FakeSession(
        [
            FakeResponse({}, status_code=429, headers={"Retry-After": "2"}),
            FakeResponse({"contacts": [{"uuid": "a"}], "meta": {"page": 1, "pages": 1}}),
        ]
    )
    waits = []
    client = CallbellClient("key", session=session, sleep=waits.append, throttle=0)

    contacts = list(client.iter_contacts())

    assert [c["uuid"] for c in contacts] == ["a"]
    assert waits == [2.0]
    assert len(session.calls) == 2


def test_client_raises_on_http_error():
    session = FakeSession([FakeResponse({}, status_code=500)])
    client = CallbellClient("key", session=session, sleep=lambda s: None, throttle=0)

    with pytest.raises(CallbellError):
        list(client.iter_contacts())


# --- Adapter: windowed "case-B" fetch ------------------------------------------


def test_fetch_collects_in_window_conversations_with_chronological_messages():
    contacts = [
        _contact("c1", name="Mario", tags=["urgente"], assignedUser="giulia@clinica.it"),
        _contact("c2", name="Lucia", assignedUser=None),
    ]
    messages = {
        # newest-first, as Callbell returns them; both within the 6h window
        "c1": [_msg("2026-07-17T11:00:00Z", "received", "ciao"),
               _msg("2026-07-17T10:00:00Z", "sent", "rispondo")],
        "c2": [_msg("2026-07-17T09:00:00Z", "sent", "ok")],
    }
    adapter = CallbellSourceAdapter(FakeClient(contacts, messages), patience=30, now=_fixed_now)

    convos = adapter.fetch_recent_conversations(window_hours=6)

    assert [c.contact_id for c in convos] == ["c1", "c2"]
    first = convos[0]
    assert first.name == "Mario"
    assert first.channel == "whatsapp"
    assert first.tags == ("urgente",)
    assert first.assigned_user == "giulia@clinica.it"
    # reversed into chronological order: 10:00 operator, then 11:00 client
    assert [m.role for m in first.messages] == [Role.OPERATORE, Role.CLIENTE]
    assert [m.text for m in first.messages] == ["rispondo", "ciao"]
    assert convos[1].assigned_user is None


def test_fetch_stops_after_patience_consecutive_out_of_window():
    contacts = [
        _contact("c1"),
        _contact("old1"),
        _contact("old2"),
        _contact("c3_late"),  # in-window but placed after the out-of-window run
    ]
    messages = {
        "c1": [_msg("2026-07-17T11:00:00Z", "received")],
        "old1": [_msg("2026-07-16T20:00:00Z", "received")],
        "old2": [_msg("2026-07-16T18:00:00Z", "received")],
        "c3_late": [_msg("2026-07-17T10:00:00Z", "received")],
    }
    client = FakeClient(contacts, messages)
    adapter = CallbellSourceAdapter(client, patience=2, now=_fixed_now)

    convos = adapter.fetch_recent_conversations(window_hours=6)

    assert [c.contact_id for c in convos] == ["c1"]
    # stopped after old1+old2; never even scanned c3_late
    assert client.messages_requested == ["c1", "old1", "old2"]
    assert client.request_count == 3


def test_fetch_returns_empty_when_nothing_recent():
    contacts = [_contact("old1"), _contact("old2")]
    messages = {
        "old1": [_msg("2026-07-16T20:00:00Z", "received")],
        "old2": [_msg("2026-07-16T18:00:00Z", "received")],
    }
    adapter = CallbellSourceAdapter(FakeClient(contacts, messages), patience=30, now=_fixed_now)

    assert adapter.fetch_recent_conversations(window_hours=6) == []


def test_build_adapter_wires_from_config():
    config = Config(
        callbell_api_key="k",
        anthropic_api_key="a",
        telegram_bot_token="t",
        telegram_allowed_user_id=1,
        supabase_url="u",
        supabase_key="s",
    )
    adapter = build_adapter(config)
    assert isinstance(adapter, CallbellSourceAdapter)
