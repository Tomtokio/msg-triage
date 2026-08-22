"""T10/PR3 — what a tap on ✅ actually does: the writes on Callbell.

This is the only module in the package that changes a real customer record, and every
line of it is written from that premise. Three operations exist and no more: add a tag
of the closed set, remove a tag **that is ours**, rename a contact. No assignment, no
closing, no message to anybody.

Two disciplines run through the whole file.

**Re-read before writing.** ``update_contact_tags`` REPLACES the list — it is an
absolute set, not a delta (verified 2026-08-01). The tags the triage saw are minutes or
hours old by the time somebody taps, so writing from them would silently erase whatever
a colleague added in between. Every tag write therefore starts from a fresh
``get_contact`` and ends by comparing Callbell's echo against what was sent — tag by tag,
byte by byte, order excluded (see :func:`_replace_tags`). The probe of 2026-08-15 closed
the *structural* question (the two views of a contact coincide); this closes the
*temporal* one, which it explicitly left open.

**A failed bookkeeping write is not a failed action.** Once the PATCH on Callbell has
gone through, the tag IS on the contact. If ``system_tags`` then refuses the row, the
honest thing to say is exactly that — not "error", which would send somebody looking for
a write that in fact happened. So post-write storage failures come back as an outcome
with the real story in it; only failures BEFORE the write, and Callbell's own, are raised
for the caller to record as ``fallita``.

Sync and blocking (requests): the Telegram handler calls it through
``asyncio.to_thread``. The client and the store are injected, so the tests exercise the
real logic with no network and no waiting.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .callbell_adapter import CallbellClient, CallbellError
from .proposal_store import ProposalStore, StoredProposal
from .proposals import SYSTEM_TAGS, Proposal, TipoProposta, followups_for
from .storage import SupabaseError

logger = logging.getLogger(__name__)

# Shown when Callbell has no name for a contact. A rename proposal on such a contact is
# precisely the interesting case, so it must read as a sentence, not as an empty gap.
NO_NAME = "(contatto senza nome)"


@dataclass(frozen=True)
class ExecutionOutcome:
    """What happened, in one Italian sentence meant for the operator.

    ``message`` is PLAIN TEXT: the caller escapes it before splicing it into the HTML of
    the proposal message. Keeping markup out of here means the executor can be read as
    the description of an action, which is what it is.

    ``ok`` false is not always an error — it also covers "I refused, and here is why".
    Either way the proposal is closed as ``fallita``: nothing was decided by the operator
    that we should remember as a decision.
    """

    ok: bool
    message: str


# --- Delivery: the questions waiting to be asked --------------------------------


def load_deliverable_with_names(
    store: ProposalStore, client: CallbellClient, *, now: datetime
) -> list[tuple[StoredProposal, str]]:
    """Ripe undelivered proposals, each paired with the contact's CURRENT name.

    The name is read now, not taken from the run that produced the proposal: it is what
    the operator will read, a rename proposal is literally about it, and a backlog row
    from days ago has no run to borrow from. One GET per proposal, on a read-only client,
    on a queue that is a handful of rows.

    A contact we cannot read is skipped, not guessed: the row stays undelivered and comes
    back at the next run, which is the correct outcome for a question we cannot phrase.
    """
    delivered: list[tuple[StoredProposal, str]] = []
    for stored in store.load_deliverable(now=now):
        try:
            contact = client.get_contact(stored.proposal.contact_id)
        except CallbellError as exc:
            logger.warning(
                "Proposta T10 non consegnata: contatto illeggibile (%s)", type(exc).__name__
            )
            continue
        delivered.append((stored, str(contact.get("name") or "") or NO_NAME))
    return delivered


# --- Execution ------------------------------------------------------------------


def _current_tags(client: CallbellClient, contact_id: str) -> tuple[str, ...]:
    """The contact's tag list as Callbell holds it RIGHT NOW."""
    return tuple(client.get_contact(contact_id).get("tags") or ())


def _replace_tags(
    client: CallbellClient, contact_id: str, new_tags: Sequence[str]
) -> None:
    """Write the absolute list and verify the echo, tag by tag and byte by byte.

    The comparison is on the SORTED lists, not on the order: Callbell's own model calls
    ``tags`` an absolute set, nothing downstream reads a position, and failing a write
    that in fact succeeded — leaving the operator with "⚠️ errore" over a tag that IS on
    the contact — would be the worse mistake of the two. What is still checked exactly is
    the thing that matters: every name survived byte for byte (a trailing space included,
    verified 2026-08-01), nothing was dropped and nothing was invented.

    ``sorted``, deliberately, and not ``set``: a set would collapse duplicates and the
    "nothing was dropped" claim would quietly become false.

    Whether Callbell reorders at all has never been verified — the 2026-08-04 cleanup only
    ever removed tags, and there the order held. So a reordering is not accepted in
    silence: it is logged, and every real write becomes a probe on the assumption.
    """
    sent = list(new_tags)
    saved = list(client.update_contact_tags(contact_id, sent).get("tags") or ())
    if sorted(saved) != sorted(sent):
        raise CallbellError(
            f"Callbell non ha salvato i tag che abbiamo mandato: inviati "
            f"{sent!r}, salvati {saved!r}"
        )
    if saved != sent:
        logger.warning(
            "Callbell ha riordinato i tag su %s: inviati %r, salvati %r",
            contact_id,
            sent,
            saved,
        )


def _tag_add(
    stored: StoredProposal,
    *,
    client: CallbellClient,
    store: ProposalStore,
    now: datetime,
) -> ExecutionOutcome:
    """Add one tag of the closed set, keeping every other tag exactly as it was.

    The closed set is re-checked here even though the rules only ever produce members of
    it: this is the last gate before a write on a real record, and "the caller wouldn't
    do that" is not a property one can read off this file.
    """
    proposal = stored.proposal
    tag = proposal.tag
    if tag not in SYSTEM_TAGS:
        return ExecutionOutcome(False, f"«{tag}» non è un tag del set di sistema.")
    current = _current_tags(client, proposal.contact_id)

    if tag in current:
        # Not a failure: the world already matches what we were asking for. The
        # system_tags row below is what makes it OURS from now on.
        done = f"Il tag «{tag}» era già sul contatto."
    else:
        # The colleagues' tags are carried over untouched and in order — this is the
        # whole defence against REPLACE — and the echo check in _replace_tags is what
        # proves they came back the same.
        _replace_tags(client, proposal.contact_id, [*current, tag])
        done = f"Tag «{tag}» aggiunto."

    try:
        store.record_system_tag(
            proposal.contact_id, tag, proposta_id=stored.id, applied_at=now
        )
    except SupabaseError:
        logger.warning("system_tags non aggiornata dopo un tag_add riuscito", exc_info=True)
        return ExecutionOutcome(
            False,
            f"{done} Stato non registrato in system_tags — verificare a mano.",
        )

    return ExecutionOutcome(True, _with_followups(proposal, store=store, now=now, done=done))


def _with_followups(
    proposal: Proposal, *, store: ProposalStore, now: datetime, done: str
) -> str:
    """Schedule the calendar removal a confirmed add earns, and say if it did not land.

    The removals are born HERE, at the confirmation, and not in the run that proposed the
    add: a scheduled removal for a tag that might never be applied would be a row nobody
    could interpret. ``Ricoverato`` earns none, and that absence is the rule — a stay ends
    when the messages say so, never because time passed.

    A follow-up that fails to persist does not undo the action: the tag is on the
    contact. It is still worth a word in the chat, because what silently goes missing is
    the automatic removal.
    """
    followups = followups_for(proposal, now=now)
    if not followups:
        return done
    try:
        store.insert_pending(followups)
    except SupabaseError:
        logger.warning("Follow-up di rimozione non programmato", exc_info=True)
        return f"{done} (promemoria di rimozione non programmato)"
    return done


def _tag_remove(
    stored: StoredProposal,
    *,
    client: CallbellClient,
    store: ProposalStore,
) -> ExecutionOutcome:
    """Remove one tag — and only if the database says it is ours.

    Invariant 1 of T10, re-checked at the moment of writing and not only at the moment of
    proposing: ``Ricoverato`` is byte-identical to the one the colleagues apply by hand,
    so the name proves nothing and the row in ``system_tags`` is the whole proof. If the
    row is gone by the time somebody taps, we refuse — untouched is always the safe end.
    """
    proposal = stored.proposal
    tag = proposal.tag
    if tag not in SYSTEM_TAGS:
        return ExecutionOutcome(False, f"«{tag}» non è un tag del set di sistema.")
    ours = store.system_tags_for([proposal.contact_id]).get(
        proposal.contact_id, frozenset()
    )
    if tag not in ours:
        return ExecutionOutcome(
            False, f"«{tag}» non risulta un tag nostro: non lo tocco."
        )

    current = _current_tags(client, proposal.contact_id)
    if tag in current:
        # Only the target leaves; everything else survives in place and in order, which
        # the echo check then confirms came back byte for byte.
        _replace_tags(
            client,
            proposal.contact_id,
            [existing for existing in current if existing != tag],
        )
        done = f"Tag «{tag}» tolto."
    else:
        done = f"Il tag «{tag}» non era più sul contatto."

    try:
        store.forget_system_tag(proposal.contact_id, tag)
    except SupabaseError:
        logger.warning("system_tags non ripulita dopo un tag_remove riuscito", exc_info=True)
        return ExecutionOutcome(
            False, f"{done} Riga in system_tags non cancellata — verificare a mano."
        )
    return ExecutionOutcome(True, done)


def _rename(stored: StoredProposal, *, client: CallbellClient) -> ExecutionOutcome:
    """Write the proposed name, and check Callbell kept it byte for byte.

    No re-read first, unlike the tags: ``name`` is an absolute value with no collaterals
    to lose, so there is nothing a stale read could destroy. The echo is verified all the
    same — accents, double spaces and a trailing space all survive (verified 2026-08-05),
    so a difference here would mean something changed on their side.
    """
    nome = str(stored.proposal.payload.get("nome", ""))
    if not nome:
        return ExecutionOutcome(False, "La proposta non porta un nome da scrivere.")
    saved = client.update_contact_name(stored.proposal.contact_id, nome)
    if str(saved.get("name") or "") != nome:
        raise CallbellError(
            f"Callbell ha normalizzato il nome: inviato {nome!r}, salvato {saved.get('name')!r}"
        )
    return ExecutionOutcome(True, f"Contatto rinominato in «{nome}».")


def execute(
    stored: StoredProposal,
    *,
    client: CallbellClient,
    store: ProposalStore,
    now: datetime,
) -> ExecutionOutcome:
    """Carry out one confirmed proposal. The caller has already claimed the row.

    Raises :class:`CallbellError` (and lets a pre-write :class:`SupabaseError` through)
    so the caller can close the proposal as ``fallita`` and show the reason. Everything
    it returns is a finished story, successful or not.
    """
    tipo = stored.proposal.tipo
    if tipo is TipoProposta.TAG_ADD:
        return _tag_add(stored, client=client, store=store, now=now)
    if tipo is TipoProposta.TAG_REMOVE:
        return _tag_remove(stored, client=client, store=store)
    return _rename(stored, client=client)
