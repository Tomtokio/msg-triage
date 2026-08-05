"""Unit tests for the rename probe. No network, no mock library.

The script is loaded by path (it lives in scripts/, not in the package) WITHOUT
touching ``sys.path``, exactly like tests/test_cleanup_stale_tags.py.

What these tests are really for: the probe is the instrument that will decide whether
T10 may rename real contacts, so a probe that reports "tutto a posto" when it should
not is worse than no probe at all. Hence the failure cases carry as much weight as the
happy path — a normalised name, a wiped collateral, a restore that never happened.
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from msg_triage.callbell_adapter import CallbellError

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "probe_rename.py"
_spec = importlib.util.spec_from_file_location("probe_rename", _SCRIPT)
probe_rename = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = probe_rename
_spec.loader.exec_module(probe_rename)

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)
PROBE_NAME = probe_rename.DEFAULT_PROBE_NAME
ORIGINAL_NAME = "Gabri92"


def _contact(**extra) -> dict:
    base = {
        "uuid": "c1",
        "name": ORIGINAL_NAME,
        "tags": ["Ricoverato"],
        "note": "prosa scritta da una collega",
        "assignedUser": "giulia@clinica.it",
        "customFields": {"orderNumber": 7},
        "phoneNumber": "39333",
    }
    base.update(extra)
    return base


class FakeClient:
    """Canned contact, recorded writes, with the surface the probe actually uses.

    ``normalise`` simulates a backend that rewrites what it is given (the exact risk
    the probe exists to catch); ``wipe`` clears a collateral on every write; ``echo_drops``
    removes fields from the PATCH echo only, which is what the real API is documented
    to do with ``note``.
    """

    def __init__(self, contact, *, normalise=None, wipe=None, echo_drops=(), read_error=None):
        self._contact = dict(contact)
        self._normalise = normalise
        self._wipe = wipe
        self._echo_drops = tuple(echo_drops)
        self._read_error = read_error
        self.patches = []
        self.reads = 0

    def get_contact(self, contact_uuid):
        self.reads += 1
        # Only the re-read after the write fails, never the initial one.
        if self._read_error is not None and self.reads > 1:
            raise self._read_error
        return dict(self._contact)

    def update_contact_name(self, contact_uuid, name):
        self.patches.append((contact_uuid, name))
        self._contact["name"] = self._normalise(name) if self._normalise else name
        if self._wipe:
            self._contact[self._wipe] = None
        return {k: v for k, v in self._contact.items() if k not in self._echo_drops}


def _probe(client, tmp_path, *, probe_name=PROBE_NAME):
    lines = []
    checks = probe_rename.probe(
        client,
        "c1",
        probe_name=probe_name,
        now=NOW,
        backup_path=tmp_path / "backup.jsonl",
        emit=lines.append,
    )
    return checks, lines


def _failed(checks):
    return [c.name for c in checks if not c.passed]


# --- compare_collaterals -------------------------------------------------------


def test_compare_collaterals_flags_a_changed_note():
    before = _contact()
    after = _contact(note=None)

    assert probe_rename.compare_collaterals(before, after) == ["note"]


def test_compare_collaterals_is_lenient_only_about_absent_fields():
    """The documented PATCH echo carries no ``note``. Absent is not cleared.

    Lenient on the echo, strict on the re-read: get the direction wrong and the probe
    either cries wolf on every run or misses a wiped note entirely.
    """
    before = _contact()
    echo = {k: v for k, v in _contact().items() if k != "note"}

    assert probe_rename.compare_collaterals(before, echo, only_present=True) == []
    assert probe_rename.compare_collaterals(before, echo) == ["note"]


def test_compare_collaterals_treats_absent_and_null_alike():
    before = {"uuid": "c1"}
    after = {"uuid": "c1", "note": None, "tags": None}

    assert probe_rename.compare_collaterals(before, after) == []


# --- The happy path ------------------------------------------------------------


def test_probe_writes_verifies_and_restores(tmp_path):
    client = FakeClient(_contact())

    checks, _ = _probe(client, tmp_path)

    assert _failed(checks) == []
    # Two writes, in order: the probe name, then the original back.
    assert client.patches == [("c1", PROBE_NAME), ("c1", ORIGINAL_NAME)]


def test_probe_leaves_the_contact_exactly_as_found(tmp_path):
    client = FakeClient(_contact())

    _probe(client, tmp_path)

    assert client._contact == _contact()


def test_backup_is_written_before_the_first_patch(tmp_path):
    """An interrupted run must leave the name to restore, not the intention to restore."""
    backup = tmp_path / "backup.jsonl"

    class RecordingClient(FakeClient):
        def update_contact_name(self, contact_uuid, name):
            # The file must already hold the original name by the time we write.
            assert backup.exists(), "backup non scritto prima della PATCH"
            return super().update_contact_name(contact_uuid, name)

    probe_rename.probe(
        RecordingClient(_contact()),
        "c1",
        probe_name=PROBE_NAME,
        now=NOW,
        backup_path=backup,
        emit=lambda line: None,
    )

    entry = json.loads(backup.read_text(encoding="utf-8").splitlines()[0])
    assert entry["name_before"] == ORIGINAL_NAME
    assert entry["probe_name"] == PROBE_NAME


def test_echo_without_note_does_not_look_like_a_wiped_note(tmp_path):
    """The real echo is documented without ``note``: that alone must not fail the probe."""
    client = FakeClient(_contact(), echo_drops=("note",))

    checks, _ = _probe(client, tmp_path)

    assert _failed(checks) == []


# --- The failures the probe exists to catch ------------------------------------


def test_a_normalised_name_fails_the_byte_for_byte_check(tmp_path):
    """A backend that strips the trailing space is exactly what we are looking for."""
    client = FakeClient(_contact(), normalise=lambda name: name.strip())

    checks, _ = _probe(client, tmp_path)

    failed = _failed(checks)
    assert any("byte per byte" in name for name in failed)
    # And it is still restored: a failed verification is not a reason to leave a
    # real contact renamed.
    assert client._contact["name"] == ORIGINAL_NAME


def test_a_wiped_collateral_fails_the_check(tmp_path):
    client = FakeClient(_contact(), wipe="note")

    checks, _ = _probe(client, tmp_path)

    assert any("collaterali" in name for name in _failed(checks))


def test_the_contact_is_restored_even_if_verification_blows_up(tmp_path):
    """The re-read dies mid-flight; the restore still runs, from the ``finally``."""
    client = FakeClient(_contact(), read_error=CallbellError("boom"))

    with pytest.raises(CallbellError):
        _probe(client, tmp_path)

    assert client.patches[-1] == ("c1", ORIGINAL_NAME)
    assert client._contact["name"] == ORIGINAL_NAME


def test_a_failed_restore_says_exactly_what_to_fix_by_hand(tmp_path):
    class RestoreFails(FakeClient):
        def update_contact_name(self, contact_uuid, name):
            result = super().update_contact_name(contact_uuid, name)
            if name == ORIGINAL_NAME:
                raise CallbellError("500")
            return result

    lines = []
    with pytest.raises(CallbellError):
        probe_rename.probe(
            RestoreFails(_contact()),
            "c1",
            probe_name=PROBE_NAME,
            now=NOW,
            backup_path=tmp_path / "backup.jsonl",
            emit=lines.append,
        )

    shouted = "\n".join(lines)
    assert "RIPRISTINO FALLITO" in shouted
    assert repr(ORIGINAL_NAME) in shouted


def test_probe_refuses_when_the_contact_already_carries_the_probe_name(tmp_path):
    """Otherwise a no-op backend would pass every check without writing a thing."""
    client = FakeClient(_contact(name=PROBE_NAME))

    with pytest.raises(CallbellError):
        _probe(client, tmp_path)

    assert client.patches == []


# --- Reporting -----------------------------------------------------------------


def test_report_returns_false_and_says_not_to_build_on_it():
    lines = []
    ok = probe_rename._report(
        [probe_rename.Check("una verifica", False, "dettaglio")], emit=lines.append
    )

    assert ok is False
    assert "QUALCOSA NON TORNA" in "\n".join(lines)
