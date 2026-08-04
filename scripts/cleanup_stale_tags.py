"""One-shot removal of the stale tags in ``TARGET_TAGS`` from Callbell contacts.

These tags are applied by colleagues but never taken off, so they have stopped
meaning anything: the census of 2026-08-04 counts ~50 `Ricoverato`, 19 `Risolto`,
11 `Noemi rispond!` and 9 `dare Appuntamento`, going back months. This script
removes them from the contacts whose last human activity is older than
``STALE_AFTER_DAYS``, and leaves the recent ones exactly as they are. Incremental
tag lifecycle is T10 — this is the one-off reset that makes the tags meaningful again.

This is the ONLY write path in the project. Everything here is built around that:

- dry-run is the default; ``--esegui-davvero`` is required to touch anything, and
  in dry-run the client is constructed read-only so a write is impossible, not
  merely unintended;
- the server-side ``tags[]`` filter matches case-insensitively, so it is used to
  FIND candidates and never trusted for correctness: every contact is re-checked
  for an exact, byte-for-byte target tag before it is touched;
- every contact is re-read immediately before its write, because most of them end
  up with an empty tag list and a blind ``[]`` would silently destroy a tag a
  colleague added in the meantime;
- the backup line is written, flushed and fsync'd BEFORE the PATCH, so an
  interrupted run leaves a complete record of what it had already changed.

A contact can carry more than one of these tags (``['Ricoverato', 'Risolto']`` is
real). Walking tag by tag and writing as we go would PATCH such a contact twice —
two windows of risk, two backup lines, two chances to race a colleague, for one
contact. So the order is inverted: discovery collects the candidates of ALL tags
first, they are deduplicated by ``contact_id``, and each contact gets exactly one
PATCH that strips every target tag found on it in a single call.

That PATCH removes only the target tags OBSERVED DURING DISCOVERY. If a colleague
adds another target tag in between, the fresh re-read sees it and leaves it alone:
it was never measured against the 14-day threshold, so it is not ours to remove.

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
from dataclasses import dataclass, field, replace
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

# The exact tags to remove, with the counts measured on 2026-08-04. Never strip()
# or lower() these — the whole class of bug here is made of characters you cannot
# see: `Noemi rispond!` really is missing its `i`, and the previous target of this
# script (`Tommaso rispondi! `, already cleaned) really did end with a space.
# The other tags in the account (Contattare Urgente, Emergenza, Inviare Fattura,
# Michela rispondi!, Stiamo Arrivando) have zero contacts and are deliberately out.
TARGET_TAGS = (
    "Ricoverato",         # ~50 contatti
    "Risolto",            # 19
    "Noemi rispond!",     # 11
    "dare Appuntamento",  # 9
)
STALE_AFTER_DAYS = 14
# Above this many candidates FOR ONE TAG the tags[] filter was clearly ignored and
# we are about to walk all ~6700 contacts. The biggest known population is ~50.
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
    """One contact returned by one tag's filter, with its measured age."""

    contact_id: str
    name: str
    tags: tuple[str, ...]
    last_message_at: datetime | None
    age_days: float | None


@dataclass
class Discovery:
    """The four buckets phase 1 sorts every contact of ONE tag into."""

    tag: str
    stale: list[Candidate] = field(default_factory=list)
    recent: list[Candidate] = field(default_factory=list)
    undatable: list[Candidate] = field(default_factory=list)
    variants: list[Candidate] = field(default_factory=list)
    fetched: int = 0


@dataclass(frozen=True)
class RemovalTarget:
    """One contact and every target tag to strip from it, in a single PATCH."""

    contact_id: str
    name: str
    tags: tuple[str, ...]  # the whole tag list as seen during discovery
    targets_present: tuple[str, ...]  # only the exact target tags to remove
    last_message_at: datetime | None
    age_days: float | None

    @property
    def tags_after(self) -> list[str]:
        return [tag for tag in self.tags if tag not in self.targets_present]

    @property
    def leaves_empty(self) -> bool:
        return not self.tags_after


@dataclass
class ExecutionReport:
    removed: int = 0  # contacts
    tags_removed: int = 0  # tags, >= removed when a contact carried more than one
    skipped: int = 0  # every target tag already gone when we re-read the contact
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


def _discover_one_tag(
    client: CallbellClient,
    tag: str,
    *,
    now: datetime,
    ages: dict[str, datetime | None],
) -> Discovery:
    """Fetch every contact carrying ``tag`` and sort it into the four buckets."""
    contacts: list[dict] = []
    for contact in client.iter_contacts_by_tag(tag):
        contacts.append(contact)
        if len(contacts) > MAX_CANDIDATES:
            raise Abort(
                f"il filtro ?tags[]={tag!r} ha restituito più di {MAX_CANDIDATES} "
                "contatti: quasi certamente è stato ignorato e stiamo per scandagliare "
                "l'intera rubrica. Niente è stato scritto."
            )

    discovery = Discovery(tag=tag, fetched=len(contacts))
    for contact in contacts:
        contact_id = contact["uuid"]
        tags = tuple(contact.get("tags") or ())
        base = {
            "contact_id": contact_id,
            "name": contact.get("name") or "",
            "tags": tags,
        }
        # Exact match only. The server filter is case-insensitive, so it can hand
        # back variants; correctness never leans on it.
        if tag not in tags:
            discovery.variants.append(
                Candidate(**base, last_message_at=None, age_days=None)
            )
            continue

        # Measured once per contact, not once per tag: a contact carrying two of
        # these tags must be classified identically under both, or the dedup below
        # would have to arbitrate between "stale" and "recent" for the same contact.
        if contact_id not in ages:
            ages[contact_id] = _last_human_activity(client, contact_id)
        last_message_at = ages[contact_id]
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


def discover(client: CallbellClient, *, now: datetime) -> list[Discovery]:
    """One Discovery per target tag, in TARGET_TAGS order."""
    ages: dict[str, datetime | None] = {}
    return [
        _discover_one_tag(client, tag, now=now, ages=ages) for tag in TARGET_TAGS
    ]


def merge_targets(discoveries: list[Discovery]) -> list[RemovalTarget]:
    """Deduplicate the stale contacts of all tags into one PATCH per contact.

    Only the ``stale`` buckets are read, so a contact that is exact under one tag
    and a case variant under another contributes only the tag it actually carries.
    First-seen order is preserved, which is what makes ``--limit`` predictable.
    """
    by_contact: dict[str, RemovalTarget] = {}
    for discovery in discoveries:
        for candidate in discovery.stale:
            existing = by_contact.get(candidate.contact_id)
            if existing is None:
                by_contact[candidate.contact_id] = RemovalTarget(
                    contact_id=candidate.contact_id,
                    name=candidate.name,
                    tags=candidate.tags,
                    targets_present=(discovery.tag,),
                    last_message_at=candidate.last_message_at,
                    age_days=candidate.age_days,
                )
            else:
                by_contact[candidate.contact_id] = replace(
                    existing,
                    targets_present=existing.targets_present + (discovery.tag,),
                )
    return list(by_contact.values())


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


def _remove_tags_from_one(
    client: CallbellClient,
    target: RemovalTarget,
    *,
    backup: BackupLog,
    now: datetime,
) -> list[str]:
    """Re-read, record, write, verify. Returns the tags actually removed.

    An empty list means every target tag was already gone: nothing was written.
    """
    fresh = client.get_contact(target.contact_id)
    fresh_tags = tuple(fresh.get("tags") or ())
    # Only what discovery verified and the contact still carries. A target tag that
    # appeared in the meantime was never measured against the threshold: not ours.
    to_remove = [tag for tag in target.targets_present if tag in fresh_tags]
    if not to_remove:
        return []

    new_tags = [tag for tag in fresh_tags if tag not in to_remove]
    # Invariants, checked as real code (asserts vanish under -O): exactly as many
    # tags fewer as we meant to remove, the targets gone, nothing else invented,
    # and every surviving tag left in place and in order.
    if len(new_tags) != len(fresh_tags) - len(to_remove):
        raise Abort(
            f"conteggio tag inatteso su {target.contact_id}: {fresh_tags!r} -> "
            f"{new_tags!r} togliendo {to_remove!r}. Niente è stato scritto su "
            "questo contatto."
        )
    if any(tag in new_tags for tag in to_remove):
        raise Abort(f"tag da rimuovere sopravvissuto su {target.contact_id}: {new_tags!r}")
    if not set(new_tags).issubset(set(fresh_tags)):
        raise Abort(f"lista nuova non è un sottoinsieme di quella vecchia: {new_tags!r}")
    if [tag for tag in fresh_tags if tag not in to_remove] != new_tags:
        raise Abort(
            f"i tag da tenere non sono rimasti intatti su {target.contact_id}: "
            f"{fresh_tags!r} -> {new_tags!r}"
        )

    backup.record(
        {
            "contact_id": target.contact_id,
            "name": target.name,
            "tags_before": list(fresh_tags),
            "tags_removed": list(to_remove),
            "last_message_at": (
                target.last_message_at.isoformat() if target.last_message_at else None
            ),
            "removed_at": now.isoformat(),
        }
    )

    saved = client.update_contact_tags(target.contact_id, new_tags)
    saved_tags = list(saved.get("tags") or ())
    # Byte-for-byte. On the multi-tag contacts this is what proves the colleagues'
    # other tags survived intact; on the rest it is [] == [] and costs nothing.
    if saved_tags != new_tags:
        raise Abort(
            f"Callbell ha normalizzato i tag su {target.contact_id}: "
            f"inviati {new_tags!r}, salvati {saved_tags!r}. Run interrotto — "
            "controlla il file di backup per sapere dove eravamo arrivati."
        )
    return to_remove


def execute_removals(
    client: CallbellClient,
    targets: list[RemovalTarget],
    *,
    backup_path: Path,
    now: datetime,
    emit=print,
) -> ExecutionReport:
    """Remove the target tags from each contact, stopping on systematic failure."""
    report = ExecutionReport()
    consecutive_failures = 0
    with BackupLog(backup_path) as backup:
        for index, target in enumerate(targets, start=1):
            try:
                removed = _remove_tags_from_one(client, target, backup=backup, now=now)
            except CallbellError as exc:
                report.failed += 1
                consecutive_failures += 1
                emit(f"  [{index}/{len(targets)}] ERRORE {target.name}: {exc}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise Abort(
                        f"{MAX_CONSECUTIVE_FAILURES} errori consecutivi: mi fermo "
                        f"dopo {report.removed} rimozioni riuscite."
                    ) from exc
                continue
            consecutive_failures = 0
            if removed:
                report.removed += 1
                report.tags_removed += len(removed)
                emit(f"  [{index}/{len(targets)}] rimosso {removed!r} — {target.name}")
            else:
                report.skipped += 1
                emit(
                    f"  [{index}/{len(targets)}] già senza tag, saltato — {target.name}"
                )
    return report


# --- Reporting -----------------------------------------------------------------


def _n(count: int, singular: str, plural: str) -> str:
    """Italian is read by a human here, so "1 contatti" is not good enough."""
    return f"{count} {singular if count == 1 else plural}"


def _format_row(candidate: Candidate | RemovalTarget) -> str:
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


def _report_one_tag(discovery: Discovery, *, emit=print) -> None:
    """Print one tag's four buckets. Tags go through repr() on purpose."""
    emit(
        f"\n=== TAG {discovery.tag!r} — "
        f"{_n(discovery.fetched, 'contatto', 'contatti')} dal filtro ==="
    )

    emit(f"\n== DA RIPULIRE — ultimo messaggio oltre {STALE_AFTER_DAYS} giorni fa "
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


def _report_discovery(discoveries: list[Discovery], *, emit=print) -> None:
    for discovery in discoveries:
        _report_one_tag(discovery, emit=emit)


def _report_overall(
    discoveries: list[Discovery], targets: list[RemovalTarget], *, emit=print
) -> None:
    """The cross-tag summary: what dedup actually merged, and what it will empty."""
    pairs = sum(len(d.stale) for d in discoveries)
    multi = [t for t in targets if len(t.targets_present) > 1]
    emit("\n=== RIEPILOGO COMPLESSIVO ===")
    emit(
        f"\nContatti da modificare: {len(targets)} "
        f"(coppie contatto-tag: {pairs} — la differenza sono i contatti con più tag)"
    )
    if multi:
        emit(f"\n== CON PIÙ DI UN TAG DA TOGLIERE ({len(multi)}) — una sola PATCH ciascuno")
        for target in multi:
            emit(f"{_format_row(target)}  → toglie {list(target.targets_present)!r}")

    empties = sum(1 for t in targets if t.leaves_empty)
    # Deduplicated across tags: the same contact can be recent under two tags.
    recent = {c.contact_id for d in discoveries for c in d.recent}
    undatable = {c.contact_id for d in discoveries for c in d.undatable}
    variants = {c.contact_id for d in discoveries for c in d.variants}
    emit(
        f"\nDi cui resteranno senza nessun tag: {empties} · "
        f"contatti recenti: {len(recent)} · "
        f"non databili: {len(undatable)} · "
        f"varianti: {len(variants)}"
    )


def _report_bodies(targets: list[RemovalTarget], *, emit=print, sample: int = 5) -> None:
    """Show the exact JSON that would be sent, so the dry-run is a review."""
    if not targets:
        return
    emit(f"\nBody che verrebbero inviati (primi {min(sample, len(targets))}):")
    for target in targets[:sample]:
        emit(
            f"  PATCH /contacts/{target.contact_id}  "
            f"{json.dumps({'tags': target.tags_after}, ensure_ascii=False)}"
            f"  # toglie {list(target.targets_present)!r}"
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
    discoveries = discover(client, now=now)
    _report_discovery(discoveries, emit=emit)

    targets = merge_targets(discoveries)
    _report_overall(discoveries, targets, emit=emit)

    if limit is not None:
        targets = targets[:limit]
        emit(
            f"\n--limit {limit}: mi fermo a "
            f"{_n(len(targets), 'contatto', 'contatti')}."
        )

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

    emit(
        f"\nESECUZIONE REALE su {_n(len(targets), 'contatto', 'contatti')} — "
        f"backup in {backup_path}"
    )
    report = execute_removals(
        client, targets, backup_path=backup_path, now=now, emit=emit
    )
    emit(
        f"\nFatto: {_n(report.removed, 'contatto pulito', 'contatti puliti')} "
        f"({_n(report.tags_removed, 'tag rimosso', 'tag rimossi')}), "
        f"{report.skipped} già puliti, {report.failed} falliti. Backup: {backup_path}"
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
        help=(
            "si ferma ai primi N contatti in totale, non per tag "
            "(usa --limit 1 per la prova sul campo)"
        ),
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
