"""Probe: does ``PATCH /contacts/:uuid`` with ``{"name": ...}`` really do what we need?

T10 proposes renaming contacts (the WhatsApp defaults — "Gabri92", "Ale", "Di ruscio" —
are useless for linking owner and patient). Before a line of that code is written, the
write has to be verified on real data, exactly as the ``tags`` write was on 2026-08-01.

The official docs are not enough here, and we know it for a fact: the SAME doc page
that lists ``name`` among the PATCH body fields also declares the response envelope as
an array of one element, which is wrong — real data returns an object. A doc that got
the shape of its own response wrong cannot be trusted on whether the string it stores
is the string you sent. Four things need checking, and only a real contact can answer:

1. the name is actually saved (the field is not silently ignored);
2. it survives BYTE FOR BYTE — accents, double spaces, trailing space. This matters
   because Callbell already proved it preserves tag names byte for byte, and T10's
   rename template produces real Italian names full of accents;
3. a partial body does not clear the collaterals — ``tags``, ``note``, ``assignedUser``,
   ``customFields``. ``note`` is the one that would hurt: it is the colleagues' prose;
4. the PATCH echo carries the same envelope as the GET.

The contact is left EXACTLY as it was found: the probe writes a test name, verifies,
then restores the original. Restoring is not politeness — it doubles as proof that the
write is repeatable, and it is what makes it acceptable to run this on a real record.

Safety, same shape as ``cleanup_stale_tags.py``:

- dry-run is the default; ``--esegui-davvero`` is required to touch anything, and in
  dry-run the client is built read-only so a write is impossible, not merely unintended;
- the original name is written, flushed and fsync'd to the backup file BEFORE the first
  PATCH, so an interrupted run leaves a record of what to restore by hand;
- the restore runs in a ``finally``: if the verification blows up halfway, the contact
  still goes back to its original name before the script exits.

Usage (from the repo root, with CALLBELL_API_KEY in the environment or .env):
    .venv/bin/python scripts/probe_rename.py --contact <uuid>                   # dry-run
    .venv/bin/python scripts/probe_rename.py --contact <uuid> --esegui-davvero
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from msg_triage.callbell_adapter import CallbellClient, CallbellError

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Accented vowels, a DOUBLE space and a TRAILING space: the three ways a name can come
# back "the same" while being a different string. A normalising backend would strip the
# trailing space and collapse the double one, and we would never notice on a plain name.
DEFAULT_PROBE_NAME = "Probe Àèìòù  T10 "

# Everything a partial PATCH must leave alone. `note` is the one that would really hurt
# (prose written by the colleagues); the others are cheap to check while we are here.
COLLATERAL_FIELDS = ("tags", "note", "assignedUser", "customFields", "phoneNumber")

BACKUP_PATH = _PROJECT_ROOT / "rename_probe_backup.jsonl"


@dataclass(frozen=True)
class Check:
    """One verified claim, with the evidence that supports or refutes it."""

    name: str
    passed: bool
    detail: str


def compare_collaterals(
    before: dict, after: dict, *, only_present: bool = False
) -> list[str]:
    """Names of the collateral fields that differ between two contact payloads.

    ``only_present`` skips fields missing from ``after``, and exists for ONE case: the
    PATCH echo. The documented echo does not carry ``note`` at all, and an absent field
    is not a cleared field — only a fresh GET can tell those apart. So the echo is
    compared leniently and the re-read strictly, which is the comparison that counts.

    Absence and ``null`` are treated as the same thing (both read as ``None``): that is
    correct in the direction that matters — a ``note`` that had text before and is
    missing after still shows up as changed.
    """
    changed = []
    for field in COLLATERAL_FIELDS:
        if only_present and field not in after:
            continue
        if before.get(field) != after.get(field):
            changed.append(field)
    return changed


def _describe(contact: dict) -> str:
    """The fields we care about, through repr(): invisible characters must be visible."""
    lines = [f"  name          {contact.get('name')!r}"]
    for field in COLLATERAL_FIELDS:
        lines.append(f"  {field:<13} {contact.get(field)!r}")
    return "\n".join(lines)


def _check_write(
    saved: dict, expected_name: str, before: dict, *, stage: str, is_echo: bool
) -> list[Check]:
    """The claims that must hold right after a write, on the echo or on a re-read.

    ``is_echo`` is passed explicitly rather than inferred from ``stage``: BOTH PATCH
    responses are echoes (the write and the restore), and tying the leniency to the
    stage's name got that wrong the first time.
    """
    actual = saved.get("name")
    changed = compare_collaterals(before, saved, only_present=is_echo)
    return [
        Check(
            f"[{stage}] il nome è quello inviato, byte per byte",
            actual == expected_name,
            f"inviato {expected_name!r}, letto {actual!r}",
        ),
        Check(
            f"[{stage}] i collaterali sono intatti",
            not changed,
            "nessun campo cambiato" if not changed else f"CAMBIATI: {changed!r}",
        ),
    ]


class BackupLog:
    """Append-only JSONL record, fsync'd before the caller touches Callbell.

    Same shape as the one in ``cleanup_stale_tags.py``: if the process dies between the
    write and the restore, this file is what says which contact to put back and to what.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def __enter__(self) -> BackupLog:
        self._handle = self._path.open("a", encoding="utf-8")
        return self

    def __exit__(self, *exc_info) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def record(self, entry: dict) -> None:
        assert self._handle is not None, "BackupLog used outside its context manager"
        self._handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())


def probe(
    client: CallbellClient,
    contact_uuid: str,
    *,
    probe_name: str,
    now: datetime,
    backup_path: Path = BACKUP_PATH,
    emit=print,
) -> list[Check]:
    """Write, verify, re-read, restore, verify again. Returns every claim checked."""
    before = client.get_contact(contact_uuid)
    original_name = before.get("name")
    emit(f"\n=== PRIMA ===\n{_describe(before)}")

    if original_name == probe_name:
        raise CallbellError(
            "il contatto si chiama già come il nome di prova: la verifica non "
            "distinguerebbe una scrittura riuscita da un no-op. Usa --nome-prova."
        )

    checks: list[Check] = []
    written = False
    with BackupLog(backup_path) as backup:
        # Before the first PATCH, not after: an interrupted run must leave behind the
        # name to restore, not the intention to have restored it.
        backup.record(
            {
                "contact_id": contact_uuid,
                "name_before": original_name,
                "probe_name": probe_name,
                "probed_at": now.isoformat(),
            }
        )
        try:
            emit(f"\n=== SCRITTURA === PATCH name -> {probe_name!r}")
            echo = client.update_contact_name(contact_uuid, probe_name)
            written = True
            # Reaching here at all proves claim 4: _unwrap_contact raises on any
            # envelope that is not {"contact": {...}}, so the echo matched the GET.
            checks.append(
                Check("[eco] envelope {'contact': {...}} come la GET", True, "sbustata senza errori")
            )
            checks.extend(
                _check_write(echo, probe_name, before, stage="eco", is_echo=True)
            )

            after = client.get_contact(contact_uuid)
            emit(f"\n=== DOPO LA SCRITTURA ===\n{_describe(after)}")
            checks.extend(
                _check_write(after, probe_name, before, stage="rilettura", is_echo=False)
            )
        finally:
            if written:
                emit(f"\n=== RIPRISTINO === PATCH name -> {original_name!r}")
                try:
                    restored = client.update_contact_name(contact_uuid, original_name)
                except CallbellError:
                    # Loudly, and before re-raising: this is the one outcome that
                    # leaves a real contact renamed, and the message has to survive
                    # even if it is the second exception of the run.
                    emit(
                        f"\n!!! RIPRISTINO FALLITO. Il contatto {contact_uuid} si chiama "
                        f"ancora {probe_name!r}. Rimettilo a mano a {original_name!r} "
                        f"(è anche in {backup_path})."
                    )
                    raise
                checks.extend(
                    _check_write(
                        restored, original_name, before, stage="ripristino", is_echo=True
                    )
                )
                final = client.get_contact(contact_uuid)
                emit(f"\n=== FINALE ===\n{_describe(final)}")
                checks.extend(
                    _check_write(final, original_name, before, stage="finale", is_echo=False)
                )
    return checks


def _report(checks: list[Check], *, emit=print) -> bool:
    """Print the verdict table. Returns True when every claim held."""
    emit("\n=== VERIFICA ===")
    for check in checks:
        emit(f"  {'OK  ' if check.passed else 'FALLITA'}  {check.name}\n           {check.detail}")
    ok = all(check.passed for check in checks)
    emit(
        "\nTutte le verifiche passate: la scrittura del nome è affidabile."
        if ok
        else "\nQUALCOSA NON TORNA. Non scrivere la rinomina di T10 su questa base: "
        "riporta l'esito e si decide insieme."
    )
    return ok


def _report_dry_run(contact: dict, contact_uuid: str, probe_name: str, *, emit=print) -> None:
    """Show the exact bodies that would be sent, so the dry-run is a real review."""
    emit(f"\n=== CONTATTO ===\n{_describe(contact)}")
    emit("\nBody che verrebbero inviati, in quest'ordine:")
    for label, name in (("scrittura", probe_name), ("ripristino", contact.get("name"))):
        emit(
            f"  PATCH /contacts/{contact_uuid}  "
            f"{json.dumps({'name': name}, ensure_ascii=False)}   # {label}"
        )
    emit(
        "\nDRY-RUN: non ho scritto niente, né su Callbell né su disco.\n"
        "Per eseguire davvero: --esegui-davvero."
    )


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contact",
        required=True,
        metavar="UUID",
        help="uuid del contatto su cui fare la prova (scegline uno tuo)",
    )
    parser.add_argument(
        "--esegui-davvero",
        dest="execute",
        action="store_true",
        help="scrive davvero (senza questo flag è un dry-run)",
    )
    parser.add_argument(
        "--nome-prova",
        dest="probe_name",
        default=DEFAULT_PROBE_NAME,
        metavar="NOME",
        help=(
            "il nome scritto durante la prova, poi ripristinato "
            f"(default: {DEFAULT_PROBE_NAME!r})"
        ),
    )
    args = parser.parse_args()

    _load_dotenv()
    # Only Callbell is needed: this probe must not require an Anthropic/Telegram/
    # Supabase setup to run. Same reasoning as cleanup_stale_tags.py.
    api_key = (os.environ.get("CALLBELL_API_KEY") or "").strip()
    if not api_key:
        print("Manca CALLBELL_API_KEY (nell'ambiente o nel .env).", file=sys.stderr)
        return 1

    # The structural guard: in dry-run the client literally cannot write.
    client = CallbellClient(api_key, allow_writes=args.execute)
    try:
        if not args.execute:
            _report_dry_run(client.get_contact(args.contact), args.contact, args.probe_name)
            return 0
        checks = probe(
            client,
            args.contact,
            probe_name=args.probe_name,
            now=datetime.now(timezone.utc),
        )
    except CallbellError as exc:
        print(f"\nErrore Callbell: {exc}", file=sys.stderr)
        print(
            f"Se la scrittura era già partita, il nome originale è in {BACKUP_PATH}.",
            file=sys.stderr,
        )
        return 2
    return 0 if _report(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
