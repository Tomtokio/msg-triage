"""Unit tests for the one-shot stale-tag cleanup script. No network, no mock library.

The script is loaded by path (it lives in scripts/, not in the package) WITHOUT
touching ``sys.path``, so it has no side effect on the other test modules and it
disappears together with the script. Same hand-rolled-fake style as
tests/test_callbell_adapter.py: the fake client is injected at the boundary.
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from msg_triage.callbell_adapter import CallbellError

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cleanup_stale_tags.py"
_spec = importlib.util.spec_from_file_location("cleanup_stale_tags", _SCRIPT)
cleanup = importlib.util.module_from_spec(_spec)
# Registered under its own name before exec: @dataclass resolves the module by
# __module__ while building the class. sys.path is deliberately left untouched.
sys.modules[_spec.name] = cleanup
_spec.loader.exec_module(cleanup)

TAG = cleanup.TARGET_TAG  # "Tommaso rispondi! " — exclamation mark AND trailing space

# Fixed "now" so the 14-day boundary is deterministic.
NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
TWENTY_DAYS_AGO = "2026-07-12T12:00:00Z"
EXACTLY_14_DAYS_AGO = "2026-07-18T12:00:00Z"
THREE_DAYS_AGO = "2026-07-29T12:00:00Z"


def _contact(uuid, *, tags=(TAG,), name="Contatto"):
    return {"uuid": uuid, "name": name, "tags": list(tags)}


def _msg(created_at, status="received"):
    return {"createdAt": created_at, "status": status, "text": "x", "uuid": "m1"}


def _system_note(created_at):
    # No uuid and from == to: provider-generated, not human activity.
    return {
        "createdAt": created_at,
        "status": "note",
        "text": "Conversation was assigned to Tommaso",
        "from": "x",
        "to": "x",
    }


class FakeClient:
    """Canned reads, recorded writes, with the same surface the script uses."""

    def __init__(self, contacts, messages_by_uuid=None, *, patch_error=None, echo=None):
        self._contacts = contacts
        self._messages = messages_by_uuid or {}
        self._patch_error = patch_error
        self._echo = echo or {}
        self.patches = []
        self.tags_by_uuid = {c["uuid"]: list(c.get("tags") or ()) for c in contacts}

    def iter_contacts_by_tag(self, tag):
        yield from self._contacts

    def iter_messages(self, contact_uuid):
        yield from self._messages.get(contact_uuid, [])

    def get_contact(self, contact_uuid):
        return {"uuid": contact_uuid, "tags": list(self.tags_by_uuid[contact_uuid])}

    def update_contact_tags(self, contact_uuid, tags):
        self.patches.append((contact_uuid, list(tags)))
        if self._patch_error is not None:
            raise self._patch_error
        self.tags_by_uuid[contact_uuid] = list(tags)
        return {"uuid": contact_uuid, "tags": self._echo.get(contact_uuid, list(tags))}


def _run(client, tmp_path, *, execute=True, limit=None):
    lines = []
    code = cleanup.run(
        client,
        now=NOW,
        execute=execute,
        limit=limit,
        backup_path=tmp_path / "backup.jsonl",
        emit=lines.append,
    )
    return code, "\n".join(lines)


# --- Phase 1: selection ---------------------------------------------------------


def test_selects_only_contacts_older_than_the_threshold():
    client = FakeClient(
        [_contact("old"), _contact("fresh"), _contact("boundary")],
        {
            "old": [_msg(TWENTY_DAYS_AGO)],
            "fresh": [_msg(THREE_DAYS_AGO)],
            "boundary": [_msg(EXACTLY_14_DAYS_AGO)],
        },
    )

    discovery = cleanup.discover(client, now=NOW)

    assert [c.contact_id for c in discovery.stale] == ["old"]
    # exactly 14 days is NOT stale: the rule is strictly older than
    assert sorted(c.contact_id for c in discovery.recent) == ["boundary", "fresh"]


def test_uses_the_most_recent_message_ignoring_system_notes():
    """A system note must not make a dead conversation look alive."""
    client = FakeClient(
        [_contact("c1")],
        {"c1": [_system_note(THREE_DAYS_AGO), _msg(TWENTY_DAYS_AGO)]},
    )

    discovery = cleanup.discover(client, now=NOW)

    assert [c.contact_id for c in discovery.stale] == ["c1"]


def test_colleague_note_counts_as_human_activity():
    human_note = {
        "createdAt": THREE_DAYS_AGO,
        "status": "note",
        "text": "richiamata io",
        "uuid": "n1",
        "from": "giulia",
        "to": "clinica",
    }
    client = FakeClient([_contact("c1")], {"c1": [human_note, _msg(TWENTY_DAYS_AGO)]})

    discovery = cleanup.discover(client, now=NOW)

    assert discovery.stale == []
    assert [c.contact_id for c in discovery.recent] == ["c1"]


def test_exact_recheck_rejects_a_case_insensitive_variant():
    """The server filter is case-insensitive; correctness never leans on it."""
    client = FakeClient(
        [_contact("exact"), _contact("variant", tags=("tommaso rispondi!",))],
        {"exact": [_msg(TWENTY_DAYS_AGO)], "variant": [_msg(TWENTY_DAYS_AGO)]},
    )

    discovery = cleanup.discover(client, now=NOW)

    assert [c.contact_id for c in discovery.stale] == ["exact"]
    assert [c.contact_id for c in discovery.variants] == ["variant"]


def test_contact_without_messages_is_left_alone():
    client = FakeClient([_contact("c1")], {"c1": []})

    discovery = cleanup.discover(client, now=NOW)

    assert discovery.stale == []
    assert [c.contact_id for c in discovery.undatable] == ["c1"]


def test_contact_with_only_system_notes_is_not_datable():
    client = FakeClient(
        [_contact("c1")],
        {"c1": [_system_note(THREE_DAYS_AGO), _system_note(TWENTY_DAYS_AGO)]},
    )

    discovery = cleanup.discover(client, now=NOW)

    assert discovery.stale == []
    assert [c.contact_id for c in discovery.undatable] == ["c1"]


def test_too_many_candidates_aborts_before_any_work():
    contacts = [_contact(f"c{i}") for i in range(cleanup.MAX_CANDIDATES + 1)]
    client = FakeClient(contacts, {})

    with pytest.raises(cleanup.Abort):
        cleanup.discover(client, now=NOW)

    assert client.patches == []


# --- Phase 2: execution ---------------------------------------------------------


def test_dry_run_writes_nothing_anywhere(tmp_path):
    client = FakeClient([_contact("c1")], {"c1": [_msg(TWENTY_DAYS_AGO)]})

    code, output = _run(client, tmp_path, execute=False)

    assert code == 0
    assert client.patches == []
    assert not (tmp_path / "backup.jsonl").exists()
    assert "DRY-RUN" in output


def test_removal_keeps_the_other_tags(tmp_path):
    client = FakeClient(
        [_contact("c1", tags=("Risolto", TAG))], {"c1": [_msg(TWENTY_DAYS_AGO)]}
    )

    code, _ = _run(client, tmp_path)

    assert code == 0
    assert client.patches == [("c1", ["Risolto"])]


def test_removal_sends_an_empty_list_when_it_was_the_only_tag(tmp_path):
    """The majority case: ~128 of the ~130 contacts carry nothing else."""
    client = FakeClient([_contact("c1")], {"c1": [_msg(TWENTY_DAYS_AGO)]})

    code, _ = _run(client, tmp_path)

    assert code == 0
    assert client.patches == [("c1", [])]


def test_backup_line_is_written_before_the_patch(tmp_path):
    client = FakeClient(
        [_contact("c1", tags=("Risolto", TAG), name="Rossi")],
        {"c1": [_msg(TWENTY_DAYS_AGO)]},
        patch_error=CallbellError("boom"),
    )

    code, _ = _run(client, tmp_path)

    assert code == 1  # the failure is reported...
    assert client.patches == [("c1", ["Risolto"])]  # ...the PATCH was attempted...
    # ...and the record survives it, with the tag list exactly as it was
    line = json.loads((tmp_path / "backup.jsonl").read_text(encoding="utf-8").strip())
    assert line["contact_id"] == "c1"
    assert line["name"] == "Rossi"
    assert line["tags_before"] == ["Risolto", TAG]
    assert line["last_message_at"].startswith("2026-07-12")


def test_limit_stops_after_the_first_contact(tmp_path):
    client = FakeClient(
        [_contact("c1"), _contact("c2")],
        {"c1": [_msg(TWENTY_DAYS_AGO)], "c2": [_msg(TWENTY_DAYS_AGO)]},
    )

    code, _ = _run(client, tmp_path, limit=1)

    assert code == 0
    assert [uuid for uuid, _ in client.patches] == ["c1"]


def test_contact_already_cleaned_is_skipped_not_rewritten(tmp_path):
    client = FakeClient([_contact("c1")], {"c1": [_msg(TWENTY_DAYS_AGO)]})
    client.tags_by_uuid["c1"] = []  # a colleague removed it between discovery and write

    code, output = _run(client, tmp_path)

    assert code == 0
    assert client.patches == []
    assert "già senza tag" in output


def test_normalised_echo_aborts_the_run(tmp_path):
    """If Callbell hands back anything but what we sent, we stop immediately."""
    client = FakeClient(
        [_contact("c1", tags=("Risolto", TAG)), _contact("c2")],
        {"c1": [_msg(TWENTY_DAYS_AGO)], "c2": [_msg(TWENTY_DAYS_AGO)]},
        echo={"c1": ["risolto"]},  # case-folded on the way back
    )

    with pytest.raises(cleanup.Abort):
        _run(client, tmp_path)

    assert [uuid for uuid, _ in client.patches] == ["c1"]  # never reached c2


def test_three_consecutive_failures_abort(tmp_path):
    contacts = [_contact(f"c{i}") for i in range(5)]
    client = FakeClient(
        contacts,
        {f"c{i}": [_msg(TWENTY_DAYS_AGO)] for i in range(5)},
        patch_error=CallbellError("boom"),
    )

    with pytest.raises(cleanup.Abort):
        _run(client, tmp_path)

    assert len(client.patches) == cleanup.MAX_CONSECUTIVE_FAILURES
