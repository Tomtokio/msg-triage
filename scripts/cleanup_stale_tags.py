"""One-shot removal of the stale `Tommaso rispondi! ` tag from Callbell contacts.

The tag is applied by colleagues but never taken off, so it has stopped meaning
anything: ~130 contacts carry it, going back to July 2025. This script removes it
from the contacts whose last human activity is older than ``STALE_AFTER_DAYS``,
and leaves the recent ones exactly as they are. Incremental tag lifecycle is T10 —
this is the one-off reset that makes the tag meaningful again.

This is the ONLY write path in the project. Everything here is built around that:

- dry-run is the default; ``--esegui-davvero`` is required to touch anything, and
  in dry-run the client is constructed read-only so a write is impossible, not
  merely unintended;
- the server-side ``tags[]`` filter matches case-insensitively, so it is used to
  FIND candidates and never trusted for correctness: every contact is re-checked
  for an exact, byte-for-byte ``TARGET_TAG`` before it is touched;
- every contact is re-read immediately before its write, because ~128 of the ~130
  end up with an empty tag list and a blind ``[]`` would silently destroy a tag a
  colleague added in the meantime;
- the backup line is written, flushed and fsync'd BEFORE the PATCH, so an
  interrupted run leaves a complete record of what it had already changed.

Restart needs no bookkeeping and none is implemented on purpose: the ``tags[]``
filter only returns contacts that still carry the tag, so the ones already cleaned
drop out of the list by themselves. After an abort, just run it again.

Usage (from the repo root, with CALLBELL_API_KEY in the environment or .env):
    .venv/bin/python scripts/cleanup_stale_tags.py                       # dry-run
    .venv/bin/python scripts/cleanup_stale_tags.py --esegui-davvero --limit 1
    .venv/bin/python scripts/cleanup_stale_tags.py --esegui-davvero
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from msg_triage.callbell_adapter import (
    CallbellClient,
    CallbellError,
    _is_system_note,
    _parse_ts,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The exact tag to remove: exclamation mark AND trailing space. Never strip() or
# lower() this — the whole class of bug here is made of characters you cannot see.
TARGET_TAG = "Tommaso rispondi! "
STALE_AFTER_DAYS = 14
# Above this many candidates the tags[] filter was clearly ignored and we are about
# to walk all ~6700 contacts. Known population is ~130, so 300 is generous.
MAX_CANDIDATES = 300
# Messages are newest-first; a contact buried under system notes must not paginate
# forever just to find one datable message.
MAX_MESSAGES_SCANNED = 50
MAX_CONSECUTIVE_FAILURES = 3
BACKUP_PATH = _PROJECT_ROOT / "tag_cleanup_backup.jsonl"
_SECONDS_PER_DAY = 86400.0


class Abort(RuntimeError):
    """Raised when the run must stop immediately, mid-flight."""


@dataclass(frozen=True)
class Candidate:
    """One contact carrying the exact target tag, with its measured age."""

    contact_id: str
    name: str
    tags: tuple[str, ...]
    last_message_at: datetime | None
    age_days: float | None

    @property
    def only_target_tag(self) -> bool:
        return self.tags == (TARGET_TAG,)


@dataclass
class Discovery:
    """The four buckets phase 1 sorts every fetched contact into."""

    stale: list[Candidate] = field(default_factory=list)
    recent: list[Candidate] = field(default_factory=list)
    undatable: list[Candidate] = field(default_factory=list)
    variants: list[Candidate] = field(default_factory=list)
    fetched: int = 0


@dataclass
class ExecutionReport:
    removed: int = 0
    skipped: int = 0  # tag already gone when we re-read the contact
    failed: int = 0


# --- Phase 1: discovery (identical in dry-run and for real) --------------------


def _last_human_activity(client: CallbellClient, contact_uuid: str) -> datetime | None:
    """Timestamp of the most recent message that is not a provider-generated note.

    Messages arrive newest-first, so the first non-system note IS the last human
    activity. System notes ("Conversation was assigned to X") are skipped: they
    would make a conversation where nothing happened look alive. A note a colleague
    actually wrote counts — that is human activity.

    Returns None when nothing datable exists; there is deliberately no fallback to
    the contact's ``createdAt``, which is the creation date and not activity.
    """
    messages = islice(client.iter_messages(contact_uuid), MAX_MESSAGES_SCANNED)
    for raw in messages:
        if _is_system_note(raw):
            continue
        created_at = raw.get("createdAt")
        if created_at:
            return _parse_ts(created_at)
    return None


def discover(client: CallbellClient, *, now: datetime) -> Discovery:
    """Fetch every contact carrying the tag and sort it into the four buckets."""
    contacts: list[dict] = []
    for contact in client.iter_contacts_by_tag(TARGET_TAG):
        contacts.append(contact)
        if len(contacts) > MAX_CANDIDATES:
            raise Abort(
                f"il filtro ?tags[]= ha restituito più di {MAX_CANDIDATES} contatti: "
                "quasi certamente è stato ignorato e stiamo per scandagliare l'intera "
                "rubrica. Niente è stato scritto."
            )

    discovery = Discovery(fetched=len(contacts))
    for contact in contacts:
        tags = tuple(contact.get("tags") or ())
        base = {
            "contact_id": contact["uuid"],
            "name": contact.get("name") or "",
            "tags": tags,
        }
        # Exact match only. The server filter is case-insensitive, so it can hand
        # back variants; correctness never leans on it.
        if TARGET_TAG not in tags:
            discovery.variants.append(
                Candidate(**base, last_message_at=None, age_days=None)
            )
            continue

        last_message_at = _last_human_activity(client, contact["uuid"])
        if last_message_at is None:
            discovery.undatable.append(
                Candidate(**base, last_message_at=None, age_days=None)
            )
            continue

        age_days = (now - last_message_at).total_seconds() / _SECONDS_PER_DAY
        candidate = Candidate(**base, last_message_at=last_message_at, age_days=age_days)
        if age_days > STALE_AFTER_DAYS:
            discovery.stale.append(candidate)
        else:
            discovery.recent.append(candidate)
    return discovery


# --- Phase 2: execution (only with --esegui-davvero) ---------------------------


class BackupLog:
    """Append-only JSONL record of what was removed, fsync'd before each write.

    Probe C confirmed Callbell preserves tag names byte for byte, trailing space
    included, so this file is a real undo and not just a report: re-sending a
    recorded ``tags_before`` restores the contact exactly.
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
        """Write one line and force it to disk BEFORE the caller touches Callbell."""
        assert self._handle is not None, "BackupLog used outside its context manager"
        self._handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())


def _remove_tag_from_one(
    client: CallbellClient,
    candidate: Candidate,
    *,
    backup: BackupLog,
    now: datetime,
) -> bool:
    """Re-read, record, write, verify. Returns False if the tag was already gone."""
    fresh = client.get_contact(candidate.contact_id)
    fresh_tags = tuple(fresh.get("tags") or ())
    if TARGET_TAG not in fresh_tags:
        return False

    new_tags = [tag for tag in fresh_tags if tag != TARGET_TAG]
    # Invariants, checked as real code (asserts vanish under -O): exactly one tag
    # fewer, the target gone, and nothing else invented.
    if len(new_tags) != len(fresh_tags) - 1 or TARGET_TAG in new_tags:
        raise Abort(
            f"tag inattesi su {candidate.contact_id}: {fresh_tags!r} -> {new_tags!r}. "
            "Niente è stato scritto su questo contatto."
        )
    if not set(new_tags).issubset(set(fresh_tags)):
        raise Abort(f"lista nuova non è un sottoinsieme di quella vecchia: {new_tags!r}")

    backup.record(
        {
            "contact_id": candidate.contact_id,
            "name": candidate.name,
            "tags_before": list(fresh_tags),
            "last_message_at": (
                candidate.last_message_at.isoformat()
                if candidate.last_message_at
                else None
            ),
            "removed_at": now.isoformat(),
        }
    )

    saved = client.update_contact_tags(candidate.contact_id, new_tags)
    saved_tags = list(saved.get("tags") or ())
    # Byte-for-byte. On the two multi-tag contacts this is what proves the
    # colleagues' tags survived intact; on the rest it is [] == [] and costs nothing.
    if saved_tags != new_tags:
        raise Abort(
            f"Callbell ha normalizzato i tag su {candidate.contact_id}: "
            f"inviati {new_tags!r}, salvati {saved_tags!r}. Run interrotto — "
            "controlla il file di backup per sapere dove eravamo arrivati."
        )
    return True


def execute_removals(
    client: CallbellClient,
    candidates: list[Candidate],
    *,
    backup_path: Path,
    now: datetime,
    emit=print,
) -> ExecutionReport:
    """Remove the tag from each candidate, stopping on systematic failure."""
    report = ExecutionReport()
    consecutive_failures = 0
    with BackupLog(backup_path) as backup:
        for index, candidate in enumerate(candidates, start=1):
            try:
                changed = _remove_tag_from_one(
                    client, candidate, backup=backup, now=now
                )
            except CallbellError as exc:
                report.failed += 1
                consecutive_failures += 1
                emit(f"  [{index}/{len(candidates)}] ERRORE {candidate.name}: {exc}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise Abort(
                        f"{MAX_CONSECUTIVE_FAILURES} errori consecutivi: mi fermo "
                        f"dopo {report.removed} rimozioni riuscite."
                    ) from exc
                continue
            consecutive_failures = 0
            if changed:
                report.removed += 1
                emit(f"  [{index}/{len(candidates)}] rimosso — {candidate.name}")
            else:
                report.skipped += 1
                emit(
                    f"  [{index}/{len(candidates)}] già senza tag, saltato — "
                    f"{candidate.name}"
                )
    return report


# --- Reporting -----------------------------------------------------------------


def _format_row(candidate: Candidate) -> str:
    # Column widths match the populated case, so the buckets line up when read together.
    when = (
        candidate.last_message_at.strftime("%Y-%m-%d")
        if candidate.last_message_at
        else "    —     "
    )
    age = (
        f"{candidate.age_days:6.0f} gg"
        if candidate.age_days is not None
        else "     —   "
    )
    return f"  {when}  {age}  {candidate.name[:28]:<28}  {list(candidate.tags)!r}"


def _report_discovery(discovery: Discovery, *, emit=print) -> None:
    """Print the dry-run review. Tags go through repr() on purpose."""
    emit(f"\nContatti restituiti dal filtro '{TARGET_TAG}': {discovery.fetched}\n")

    emit(f"== DA RIPULIRE — ultimo messaggio oltre {STALE_AFTER_DAYS} giorni fa "
         f"({len(discovery.stale)})")
    for candidate in discovery.stale:
        emit(_format_row(candidate))

    if discovery.recent:
        emit(f"\n== RECENTI, NON SI TOCCANO ({len(discovery.recent)})")
        for candidate in discovery.recent:
            emit(_format_row(candidate))

    if discovery.undatable:
        emit(
            f"\n== SENZA MESSAGGI DATABILI, NON SI TOCCANO ({len(discovery.undatable)})"
            "\n   Nessun messaggio, o solo note di sistema. Guardali tu: qui non si "
            "tira a indovinare."
        )
        for candidate in discovery.undatable:
            emit(_format_row(candidate))

    if discovery.variants:
        emit(
            f"\n== VARIANTE DEL TAG, NON SI TOCCANO ({len(discovery.variants)})"
            "\n   Restituiti dal filtro case-insensitive ma senza il tag esatto."
        )
        for candidate in discovery.variants:
            emit(_format_row(candidate))

    only_target = sum(1 for c in discovery.stale if c.only_target_tag)
    emit(
        f"\nRiepilogo: {len(discovery.stale)} da ripulire "
        f"(di cui {only_target} con SOLO questo tag → lista vuota) · "
        f"recenti: {len(discovery.recent)} · "
        f"non databili: {len(discovery.undatable)} · "
        f"varianti: {len(discovery.variants)}"
    )


def _report_bodies(candidates: list[Candidate], *, emit=print, sample: int = 5) -> None:
    """Show the exact JSON that would be sent, so the dry-run is a review."""
    if not candidates:
        return
    emit(f"\nBody che verrebbero inviati (primi {min(sample, len(candidates))}):")
    for candidate in candidates[:sample]:
        new_tags = [tag for tag in candidate.tags if tag != TARGET_TAG]
        emit(
            f"  PATCH /contacts/{candidate.contact_id}  "
            f"{json.dumps({'tags': new_tags}, ensure_ascii=False)}"
        )


# --- Orchestration -------------------------------------------------------------


def run(
    client: CallbellClient,
    *,
    now: datetime,
    execute: bool,
    limit: int | None = None,
    backup_path: Path = BACKUP_PATH,
    emit=print,
) -> int:
    discovery = discover(client, now=now)
    _report_discovery(discovery, emit=emit)

    targets = discovery.stale
    if limit is not None:
        targets = targets[:limit]
        emit(f"\n--limit {limit}: mi fermo ai primi {len(targets)}.")

    if not execute:
        _report_bodies(targets, emit=emit)
        emit(
            "\nDRY-RUN: non ho scritto niente, né su Callbell né su disco.\n"
            "Per eseguire davvero: --esegui-davvero (eventualmente con --limit 1)."
        )
        return 0

    if not targets:
        emit("\nNiente da rimuovere.")
        return 0

    emit(f"\nESECUZIONE REALE su {len(targets)} contatti — backup in {backup_path}")
    report = execute_removals(
        client, targets, backup_path=backup_path, now=now, emit=emit
    )
    emit(
        f"\nFatto: {report.removed} rimossi, {report.skipped} già puliti, "
        f"{report.failed} falliti. Backup: {backup_path}"
    )
    return 1 if report.failed else 0


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--esegui-davvero",
        dest="execute",
        action="store_true",
        help="esegue davvero le rimozioni (senza questo flag è un dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="si ferma ai primi N contatti (usa --limit 1 per la prova sul campo)",
    )
    args = parser.parse_args()

    _load_dotenv()
    # Only Callbell is needed here, so we read the one key instead of load_config():
    # this script must not require an Anthropic/Telegram/Supabase setup to run.
    api_key = (os.environ.get("CALLBELL_API_KEY") or "").strip()
    if not api_key:
        print("Manca CALLBELL_API_KEY (nell'ambiente o nel .env).", file=sys.stderr)
        return 1

    # The structural guard: in dry-run the client literally cannot write.
    client = CallbellClient(api_key, allow_writes=args.execute)
    try:
        return run(
            client,
            now=datetime.now(timezone.utc),
            execute=args.execute,
            limit=args.limit,
        )
    except Abort as exc:
        print(f"\nINTERROTTO: {exc}", file=sys.stderr)
        return 2
    except CallbellError as exc:
        print(f"\nErrore Callbell: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
