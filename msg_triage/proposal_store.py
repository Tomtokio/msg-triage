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

Scope note: only the three operations PR2 uses live here. ``claim``/``mark`` (the
double-tap defence and the outcome of a tap) and ``record_system_tag``/
``forget_system_tag`` arrive with the executor in PR3; the maturation query arrives
with the job in PR4. Methods written before their caller cannot be tested against real
use, and tend to end up with the wrong signature.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .proposals import Decision, Proposal, StatoProposta, build_proposals
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

# Logged once per process, not once per run: a flag left on without Supabase is a
# configuration mistake to fix, not a per-triage complaint.
_WARNED_WITHOUT_SUPABASE = False


@dataclass(frozen=True)
class StoredProposal:
    """A proposal with the id it was written under.

    The id is generated client-side (the house pattern, see ``save_triage_run``): no
    row is read back, and PR3 already has the value it needs for the button's
    ``callback_data``.
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


class ProposalStore:
    """The three reads and the one write PR2 needs, over a :class:`SupabaseStore`."""

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

        ``created_at`` is left to the database default; ``decided_at``, ``executed_at``
        and ``telegram_message_id`` stay NULL until somebody taps (PR3).
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

    Returns the rows written, which PR3 will turn into Telegram messages with buttons.
    Raises on any Supabase failure: see the module docstring — an unremembered proposal
    must not exist. With the flag off (or Supabase unconfigured) it returns immediately,
    touching no network at all.
    """
    if not proposals_enabled(config):
        return []

    if store is None:
        base = build_store(config)
        if base is None:  # unreachable: proposals_enabled already checked is_configured
            return []
        store = ProposalStore(base)

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
