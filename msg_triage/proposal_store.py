"""T10 — the proposals' side of Supabase: what was already proposed, what is ours.

Sits on top of :class:`~msg_triage.storage.SupabaseStore` and speaks the domain of
:mod:`msg_triage.proposals`: it reads the two things the deterministic rules cannot
know by looking at a conversation (which proposals already exist for these contacts,
and which tags WE applied), and it writes the new ones as ``pending``.

**These errors are not silent, and that is the point.** ``save_triage_run`` swallows
everything because a storage hiccup must not undo a triage that has already been
delivered. Here the opposite holds: a proposal we cannot remember making must not
exist, because the whole idempotence of T10 lives in the database — refuse it once and
it must stay refused. So the store raises, and the caller decides what to say.

Scope note: PR3 added the delivery and tap side — ``load_deliverable``,
``mark_delivered``, ``claim``, ``mark_outcome`` and the two ``system_tags`` writers.
What is still absent is PR4's maturation job, which delivers ripe proposals without a
``/triage``. Methods written before their caller cannot be tested against real use, and
tend to end up with the wrong signature.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .proposals import (
    Decision,
    Proposal,
    StatoProposta,
    TipoProposta,
    build_proposals,
)
from .source_adapter import Conversation
from .storage import SupabaseStore, build_store, is_configured
from .triage_engine import TriageResult

logger = logging.getLogger(__name__)

PROPOSALS_TABLE = "proposals"
SYSTEM_TAGS_TABLE = "system_tags"

# Exactly the columns should_propose() reads, and exactly the two states it reacts to.
# Narrowing the query here rather than filtering in Python keeps the payload bounded by
# "how many open or refused proposals this contact has", which is small by construction.
_DECISION_COLUMNS = "contact_id,tipo,payload,stato,created_at,decided_at"
_DECISION_STATES = (StatoProposta.PENDING.value, StatoProposta.RIFIUTATA.value)

# Everything needed to rebuild a Proposal and to talk about it on Telegram. `stato` is
# not among them on purpose: the query already pins it, and a column read but unused is
# a column somebody later trusts.
_DELIVERY_COLUMNS = "id,contact_id,tipo,payload,motivo,matures_at"

# Logged once per process, not once per run: a flag left on without Supabase is a
# configuration mistake to fix, not a per-triage complaint.
_WARNED_WITHOUT_SUPABASE = False


@dataclass(frozen=True)
class StoredProposal:
    """A proposal with the id it was written under.

    The id is generated client-side (the house pattern, see ``save_triage_run``): no
    row is read back, and the value is what travels in the button's ``callback_data``.
    """

    id: str
    proposal: Proposal


def _in_filter(values: Iterable[str]) -> str:
    """PostgREST ``in.("a","b")``, deduplicated and order-stable.

    Values are quoted because PostgREST otherwise treats a comma inside one as a
    separator. Contact ids are Callbell uuids, so nothing here can contain a quote.
    """
    unique = list(dict.fromkeys(values))
    return "in.(" + ",".join(f'"{value}"' for value in unique) + ")"


def _parse_ts(value) -> datetime | None:
    """A PostgREST timestamptz as an aware datetime, or ``None`` if unusable.

    Naive values are read as UTC. A cooldown compared against a naive datetime raises
    ``TypeError`` deep inside a rule, which would cost the whole run for a field that is
    only ever a hint.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _tag_of(payload) -> str:
    """The tag a stored row is about; ``""`` for a rename or an unreadable payload."""
    return str(payload.get("tag", "")) if isinstance(payload, dict) else ""


def _eq_filter(value: str) -> str:
    """PostgREST ``eq."..."``, quoted.

    The quotes are not decoration: two of the three system tags carry a space
    (``Dimissione oggi``, ``Da gestire subito``), and a filter value is parsed before it
    is compared. Same reasoning as :func:`_in_filter`.
    """
    return f'eq."{value}"'


def row_to_stored(row: dict) -> StoredProposal | None:
    """A ``proposals`` row rebuilt as a :class:`StoredProposal`, or ``None``.

    ``None`` — never an exception — for a row this code cannot read: an unknown ``tipo``
    (a future type, a hand-edited row), a missing id, a payload that is not an object.
    Both the delivery and the tap go through here — which is why it is public — so a row
    we cannot interpret behaves the same way in both moments: skipped on the way out, and
    marked ``fallita`` rather than crashing the handler once it has already been claimed.
    """
    proposal_id = row.get("id")
    contact_id = row.get("contact_id")
    if not proposal_id or not contact_id:
        logger.warning("Proposta T10 illeggibile: id o contact_id mancante")
        return None
    try:
        tipo = TipoProposta(row.get("tipo"))
    except ValueError:
        logger.warning("Proposta T10 con tipo sconosciuto: %r", row.get("tipo"))
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict):
        logger.warning("Proposta T10 con payload illeggibile (tipo %s)", tipo.value)
        return None
    return StoredProposal(
        id=str(proposal_id),
        proposal=Proposal(
            contact_id=str(contact_id),
            tipo=tipo,
            payload=payload,
            motivo=str(row.get("motivo") or ""),
            matures_at=_parse_ts(row.get("matures_at")),
        ),
    )


class ProposalStore:
    """The proposals' side of the database: what was proposed, what is ours, what was decided."""

    def __init__(self, store: SupabaseStore) -> None:
        self._store = store

    def load_decisions(self, contact_ids: Sequence[str]) -> tuple[Decision, ...]:
        """Every open or refused proposal for these contacts, newest first.

        Newest first because the cooldown looks at the most recent refusal; the rest of
        the ordering does not matter to :func:`~msg_triage.proposals.should_propose`,
        which scans the whole list.
        """
        if not contact_ids:
            return ()
        rows = self._store.select(
            PROPOSALS_TABLE,
            {
                "select": _DECISION_COLUMNS,
                "contact_id": _in_filter(contact_ids),
                "stato": _in_filter(_DECISION_STATES),
                "order": "created_at.desc",
            },
        )
        return tuple(
            Decision(
                contact_id=row.get("contact_id") or "",
                tipo=row.get("tipo") or "",
                tag=_tag_of(row.get("payload")),
                stato=row.get("stato") or "",
                created_at=_parse_ts(row.get("created_at")),
                decided_at=_parse_ts(row.get("decided_at")),
            )
            for row in rows
        )

    def system_tags_for(self, contact_ids: Sequence[str]) -> dict[str, frozenset[str]]:
        """The tags WE applied, per contact — invariant 1 of T10.

        A contact absent from the result carries none of ours, whatever its tag list
        looks like: ``Ricoverato`` written by a colleague is byte-identical to ours and
        the name alone proves nothing.
        """
        if not contact_ids:
            return {}
        rows = self._store.select(
            SYSTEM_TAGS_TABLE,
            {"select": "contact_id,tag", "contact_id": _in_filter(contact_ids)},
        )
        collected: dict[str, set[str]] = {}
        for row in rows:
            contact_id, tag = row.get("contact_id"), row.get("tag")
            if contact_id and tag:
                collected.setdefault(contact_id, set()).add(tag)
        return {contact_id: frozenset(tags) for contact_id, tags in collected.items()}

    def insert_pending(
        self, proposals: Sequence[Proposal], *, new_id=uuid.uuid4
    ) -> list[StoredProposal]:
        """Write every proposal as ``pending`` in one bulk insert.

        ``created_at`` is left to the database default; ``telegram_message_id`` stays NULL
        until the proposal is delivered, ``decided_at``/``executed_at`` until it is tapped.
        """
        if not proposals:
            return []
        stored = [StoredProposal(id=str(new_id()), proposal=p) for p in proposals]
        self._store.insert(
            PROPOSALS_TABLE,
            [
                {
                    "id": item.id,
                    "contact_id": item.proposal.contact_id,
                    "tipo": item.proposal.tipo.value,
                    "payload": item.proposal.payload,
                    "motivo": item.proposal.motivo,
                    "stato": StatoProposta.PENDING.value,
                    "matures_at": (
                        item.proposal.matures_at.isoformat()
                        if item.proposal.matures_at is not None
                        else None
                    ),
                }
                for item in stored
            ],
        )
        return stored

    # --- Delivery and tap (PR3) ------------------------------------------------

    def load_deliverable(self, *, now: datetime) -> list[StoredProposal]:
        """Every proposal waiting to be shown, oldest first.

        Three conditions, and each earns its place. ``pending``: a decided proposal has
        nothing left to ask. ``telegram_message_id is null``: a row is delivered ONCE —
        its buttons keep working in the chat forever, so re-sending would only produce a
        second copy of the same question. Ripe (``matures_at`` null or already past): an
        immediate proposal is ripe by construction, a scheduled removal only when its
        morning has come.

        Oldest first because the queue is a queue: the four rows PR2 left behind are the
        first thing the operator should see.
        """
        rows = self._store.select(
            PROPOSALS_TABLE,
            {
                "select": _DELIVERY_COLUMNS,
                "stato": f"eq.{StatoProposta.PENDING.value}",
                "telegram_message_id": "is.null",
                "or": f'(matures_at.is.null,matures_at.lte."{now.isoformat()}")',
                "order": "created_at.asc",
            },
        )
        return [stored for stored in map(row_to_stored, rows) if stored is not None]

    def mark_delivered(self, proposal_id: str, telegram_message_id: int) -> None:
        """Record which Telegram message carries this proposal's buttons.

        Called AFTER the send, never before: the column is what keeps a row from being
        delivered twice, and setting it first would turn a failed send into a proposal
        that is delivered-forever and seen-never.
        """
        self._store.patch(
            PROPOSALS_TABLE,
            {"id": _eq_filter(proposal_id)},
            {"telegram_message_id": telegram_message_id},
        )

    def claim(
        self, proposal_id: str, *, stato: StatoProposta, decided_at: datetime
    ) -> dict | None:
        """Take a pending proposal to ``stato``, atomically. ``None`` if it was gone.

        The double-tap defence, and it lives in the database because that is the only
        place two taps can meet: the filter carries ``stato=eq.pending``, so the second
        tap matches no row and gets ``None`` back instead of executing a write twice.
        ``return=representation`` means the winner also gets the row it won, which is
        exactly what the executor needs and saves a second read.

        ``decided_at`` is when the operator tapped — not ``created_at``, which is when we
        asked. The 30-day cooldown on a refused tag measures from here.
        """
        rows = self._store.patch(
            PROPOSALS_TABLE,
            {
                "id": _eq_filter(proposal_id),
                "stato": f"eq.{StatoProposta.PENDING.value}",
            },
            {"stato": stato.value, "decided_at": decided_at.isoformat()},
            prefer="return=representation",
        )
        return rows[0] if rows else None

    def mark_outcome(
        self,
        proposal_id: str,
        *,
        stato: StatoProposta,
        executed_at: datetime | None = None,
    ) -> None:
        """Close a claimed proposal as ``eseguita`` or ``fallita``.

        ``fallita`` does not block a later proposal (``should_propose`` only reacts to
        ``pending`` and ``rifiutata``): a Callbell hiccup is not a decision, and the same
        situation may well earn the same question at the next run.
        """
        row: dict = {"stato": stato.value}
        if executed_at is not None:
            row["executed_at"] = executed_at.isoformat()
        self._store.patch(PROPOSALS_TABLE, {"id": _eq_filter(proposal_id)}, row)

    def record_system_tag(
        self, contact_id: str, tag: str, *, proposta_id: str, applied_at: datetime
    ) -> None:
        """Write down that THIS tag on THIS contact is ours — invariant 1 of T10.

        An upsert, not a plain insert: the add gate reads the contact's tag list, not
        ``system_tags``, so a row can already be here (a colleague removed the tag by
        hand, the rule proposed it again). A 409 there would report as failed a proposal
        whose write on Callbell had already gone through.
        """
        self._store.insert(
            SYSTEM_TAGS_TABLE,
            [
                {
                    "contact_id": contact_id,
                    "tag": tag,
                    "applied_at": applied_at.isoformat(),
                    "proposta_id": proposta_id,
                }
            ],
            on_conflict="contact_id,tag",
        )

    def forget_system_tag(self, contact_id: str, tag: str) -> None:
        """Drop the row: ``system_tags`` is current state, so removing IS deleting.

        The history is not lost — it is in ``proposals``, row by row with its reason and
        its outcome, which is why migration 0002 gave this table a unique constraint
        instead of letting it grow into a second, diverging story.
        """
        self._store.delete(
            SYSTEM_TAGS_TABLE,
            {"contact_id": _eq_filter(contact_id), "tag": _eq_filter(tag)},
        )


# --- Gate and entry point ------------------------------------------------------


def proposals_enabled(config: Config) -> bool:
    """The flag AND a real Supabase. Both, or no proposals at all.

    Without the database there is no idempotence: a refusal would be forgotten and the
    same proposal would come back at every run, which is worse than no feature. The
    warning fires once per process because it names a configuration to fix, not a
    problem with this particular triage.
    """
    global _WARNED_WITHOUT_SUPABASE
    if not config.enable_proposals:
        return False
    if not is_configured(config):
        if not _WARNED_WITHOUT_SUPABASE:
            logger.warning(
                "ENABLE_PROPOSALS è acceso ma Supabase non è configurato: "
                "niente proposte (senza DB non c'è idempotenza)."
            )
            _WARNED_WITHOUT_SUPABASE = True
        return False
    return True


def build_proposal_store(config: Config) -> ProposalStore | None:
    """The store ready to use, or ``None`` when T10 must not touch anything.

    One place for the whole precondition — the flag AND a real Supabase — so delivery,
    the tap and the run all fall silent together. A caller that gets ``None`` has nothing
    to do; it does not need to know which half of the gate was missing (the log said).
    """
    if not proposals_enabled(config):
        return None
    base = build_store(config)
    return ProposalStore(base) if base is not None else None


def _by_type(proposals: Sequence[Proposal]) -> str:
    """Counts per type for the log line. Counts only: no names, no contact ids."""
    counts: dict[str, int] = {}
    for proposal in proposals:
        counts[proposal.tipo.value] = counts.get(proposal.tipo.value, 0) + 1
    return ", ".join(f"{tipo} {count}" for tipo, count in sorted(counts.items()))


def build_and_store_proposals(
    config: Config,
    result: TriageResult,
    conversations: Sequence[Conversation],
    *,
    store: ProposalStore | None = None,
    now: datetime | None = None,
    new_id=uuid.uuid4,
) -> list[StoredProposal]:
    """Build this run's proposals and persist them as ``pending``. Nothing is delivered.

    Returns the rows written. Delivering them is a separate step reading from the
    database (``load_deliverable``), so the queue is never just this run's.
    Raises on any Supabase failure: see the module docstring — an unremembered proposal
    must not exist. With the flag off (or Supabase unconfigured) it returns immediately,
    touching no network at all.
    """
    if not proposals_enabled(config):
        return []

    if store is None:
        store = build_proposal_store(config)
        if store is None:  # unreachable: proposals_enabled already checked is_configured
            return []

    moment = now if now is not None else datetime.now(timezone.utc)
    contact_ids = [entry.contact_id for entry in result.conversations]
    if not contact_ids:
        return []

    proposals = build_proposals(
        result.conversations,
        conversations,
        system_tags=store.system_tags_for(contact_ids),
        decisions=store.load_decisions(contact_ids),
        now=moment,
    )
    if not proposals:
        logger.info("Proposte T10: nessuna da creare su %d conversazioni", len(contact_ids))
        return []

    stored = store.insert_pending(proposals, new_id=new_id)
    logger.info("Proposte T10: %d create in stato pending (%s)", len(stored), _by_type(proposals))
    return stored
