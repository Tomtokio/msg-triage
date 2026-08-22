"""T10 — deterministic rules: state facts -> typed proposals.

This is the layer that keeps the model out of the decision. The triage engine
extracts what the messages SAY (``fatti``, PR1); everything here is plain code that
turns those statements plus the conversation's current tags into typed
:class:`Proposal` objects. The model never proposes an action, never names a tag and
never writes anything: it only reports, and this module decides.

Pure by construction: no network, no config, no clock of its own (``now`` is
injected). It consumes only the neutral :class:`~msg_triage.source_adapter.Conversation`
format and the triage domain, so nothing Callbell-specific reaches a rule.

Three invariants govern the whole file.

1. **A tag is "ours" only if a row exists in ``system_tags``, never by name.**
   ``Ricoverato`` is byte-identical to the tag colleagues already apply by hand
   (~50 contacts at the 2026-08-04 census), so the name distinguishes nothing.
   Accepted consequence: the semantic rule never removes THEIR instances; only the
   14-day net (PR4) reaches those.
2. **A tag is never removed because time passed.** A long stay with a silent chat
   must keep its ``Ricoverato``. Removal comes from what the messages say, or from a
   calendar rule attached to a tag WE applied (see :func:`followups_for`).
3. **Nothing is inferred.** Anything the model gives us that is used for arithmetic
   (a maturation date) or written onto a real customer record (a contact name) passes
   a plausibility check first. A value we do not recognise produces NO proposal —
   never a wrong one.

The proposal text shown on Telegram lives in ``renderers.render_proposal``, next to the
other renderers, which own the HTML escaping. What lives here is ``motivo``: the short
factual Italian phrase that is stored on the row and explains why the rule fired.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from .source_adapter import Conversation
from .triage_engine import (
    CLINIC_TZ,
    Animale,
    ConversationTriage,
    Fatti,
    Gruppo,
    Ricovero,
    StatoDimissione,
)

logger = logging.getLogger(__name__)

_TZ = ZoneInfo(CLINIC_TZ)

# --- The closed set of tags this system manages --------------------------------
#
# Readable for the colleagues, who see them in the Callbell UI: they are not internal
# identifiers. NEVER strip() or lower() these, and never compare them case-insensitively
# against a contact's tags -- same discipline as TARGET_TAGS in
# scripts/cleanup_stale_tags.py, where the whole class of bug is made of characters you
# cannot see. Callbell's ?tags[]= filter IS case-insensitive; that is a way to find
# candidates, never a statement of what a contact carries.

TAG_RICOVERATO = "Ricoverato"
TAG_DIMISSIONE_OGGI = "Dimissione oggi"
TAG_DA_GESTIRE_SUBITO = "Da gestire subito"

SYSTEM_TAGS: tuple[str, ...] = (
    TAG_RICOVERATO,
    TAG_DIMISSIONE_OGGI,
    TAG_DA_GESTIRE_SUBITO,
)

# How long a refused TAG stays refused. A refused rename is forever (see
# should_propose): a name we got wrong once we will get wrong again, whereas a tag
# refused in July can be right in September because the situation changed.
TAG_REFUSAL_COOLDOWN = timedelta(days=30)

# How long "Da gestire subito" lives before it is re-examined.
SUBITO_TTL = timedelta(hours=48)

# The morning at which every calendar maturation lands, clinic time. One convention for
# both calendar rules: two twin rules with two different hours is the kind of
# inconsistency that gets paid for months later, and midnight would deliver a proposal
# in the middle of the night for something read in the morning.
MATURATION_HOUR = 7


# --- Types ---------------------------------------------------------------------


class TipoProposta(str, Enum):
    """The three actions T10 may ever propose. Values match the DB check constraint."""

    TAG_ADD = "tag_add"
    TAG_REMOVE = "tag_remove"
    RENAME = "rename"


class StatoProposta(str, Enum):
    """Lifecycle of a proposal row. Values match the DB check constraint."""

    PENDING = "pending"
    APPROVATA = "approvata"
    RIFIUTATA = "rifiutata"
    ESEGUITA = "eseguita"
    FALLITA = "fallita"


ProposalKey = tuple[str, str, str]


@dataclass(frozen=True)
class Proposal:
    """One action to propose, with the reason that produced it.

    ``payload`` is the jsonb column: ``{"tag": ...}`` for the tag actions (plus
    ``"quando"`` on ``Dimissione oggi``, so the follow-up computes its deadline from the
    discharge date and not from the moment you happened to tap), ``{"nome": ...}`` for a
    rename. It is treated as immutable — the dataclass is frozen, the dict is never
    mutated after construction.

    ``matures_at`` is ``None`` for an immediate proposal (delivered inline with the
    triage) and a timestamp for a scheduled one (PR4's job picks it up when it ripens).
    """

    contact_id: str
    tipo: TipoProposta
    payload: dict
    motivo: str
    matures_at: datetime | None = None

    @property
    def tag(self) -> str:
        """The tag this proposal is about, or ``""`` for a rename."""
        if self.tipo is TipoProposta.RENAME:
            return ""
        return str(self.payload.get("tag", ""))

    @property
    def key(self) -> ProposalKey:
        """Identity for idempotence.

        A rename is identified by the CONTACT, not by the name proposed: refusing one
        means "never rename this contact", whatever we would come up with next time. A
        tag proposal is identified by its tag, so refusing ``Ricoverato`` says nothing
        about ``Dimissione oggi``.
        """
        return (self.contact_id, self.tipo.value, self.tag)


@dataclass(frozen=True)
class Decision:
    """What the database already knows about one (contact, action) pair.

    Built by the store from a ``proposals`` row. ``when`` prefers ``decided_at`` — the
    moment you tapped — and falls back to ``created_at``: the cooldown must count from
    the decision, or a proposal that sat in the queue for a week would come back a week
    too early.
    """

    contact_id: str
    tipo: str
    tag: str
    stato: str
    created_at: datetime | None = None
    decided_at: datetime | None = None

    @property
    def key(self) -> ProposalKey:
        return (self.contact_id, self.tipo, self.tag)

    @property
    def when(self) -> datetime | None:
        return self.decided_at or self.created_at


# --- What we are willing to accept from the model ------------------------------
#
# The facts have never been exercised on real data (PR1's A/B had one conversation, no
# admissions, no discharges), so these bounds are not defensive decoration: they are the
# difference between "the rule did not fire" and "the rule wrote 2025-01-01 onto a real
# contact". Deterministico prima di inferenza.

_MAX_PAST_DAYS = 30  # a discharge announced a month ago is not a schedule
_MAX_FUTURE_DAYS = 60  # nor is one two months out: both mean the date is not what we think
_MAX_NAME_TOKENS = 4  # "Maria Grazia De Santis" fits; beyond that it is prose, not a name
_MAX_NAME_LEN = 60
_MAX_ANIMAL_TOKENS = 2
_MAX_ANIMAL_LEN = 30


def _clean(value: str | None) -> str:
    """Collapse whitespace; ``None`` and blank become ``""``."""
    return " ".join(value.split()) if value else ""


def _is_plausible_person_name(name: str) -> bool:
    """A cleaned string we are willing to write into a contact's ``name``."""
    if not name or len(name) > _MAX_NAME_LEN:
        return False
    if any(char.isdigit() for char in name):
        return False
    return 1 <= len(name.split()) <= _MAX_NAME_TOKENS


def _is_plausible_animal_word(word: str) -> bool:
    """A species or an animal's name, short enough to belong in a contact name.

    Rejects the shapes a model reaches for when it has nothing: "non specificato",
    "cane e gatto", "" — none of which may end up in front of a colleague.
    """
    if not word or len(word) > _MAX_ANIMAL_LEN:
        return False
    if any(char.isdigit() for char in word):
        return False
    return len(word.split()) <= _MAX_ANIMAL_TOKENS


def _contains_word(haystack: str, needle: str) -> bool:
    """Whole-word, case-insensitive containment.

    ``(?<![\\w-])``/``(?![\\w-])`` rather than ``\\b``, the same idiom the renderers use:
    it keeps "Bunny" from matching inside "Bunnyworth" while still matching in a
    hyphenated or punctuated neighbour.
    """
    pattern = rf"(?<![\w-]){re.escape(needle)}(?![\w-])"
    return re.search(pattern, haystack, re.IGNORECASE) is not None


# --- Dates: one clock, the clinic's -------------------------------------------


def today_in_clinic(now: datetime) -> date:
    """Today in Europe/Rome — the same "oggi" the facts block gave the model.

    Resolving it in UTC would disagree with the prompt for two hours around midnight,
    and ``Dimissione oggi`` is precisely a same-day rule.
    """
    return now.astimezone(_TZ).date()


def next_morning(day: date) -> datetime:
    """07:00 Europe/Rome on the day AFTER ``day``.

    The single place a calendar maturation is computed, shared by the ``Ricoverato``
    scheduled removal and the ``Dimissione oggi`` follow-up.
    """
    return datetime.combine(day + timedelta(days=1), time(MATURATION_HOUR), tzinfo=_TZ)


def _parse_day(value) -> date | None:
    """``YYYY-MM-DD`` or nothing. The engine already validated it; this is the reader."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _plausible_day(value, now: datetime) -> date | None:
    """A discharge date we are willing to do arithmetic on, or ``None``.

    A date far outside the window around today does not mean "a distant discharge", it
    means the field is not what we think it is. No scheduled proposal beats a proposal
    that ripens in 2027.
    """
    day = _parse_day(value)
    if day is None:
        return None
    delta = (day - today_in_clinic(now)).days
    if not -_MAX_PAST_DAYS <= delta <= _MAX_FUTURE_DAYS:
        # No contact_id, no name: this line goes to journald.
        logger.warning("Data di dimissione fuori scala (%+d giorni); ignorata", delta)
        return None
    return day


def _it_date(day: date) -> str:
    """A date as an Italian reader expects it (the motivo is read by a human)."""
    return day.strftime("%d/%m/%Y")


# --- Reading the facts ---------------------------------------------------------


def _discharge_done(fatti: Fatti) -> bool:
    """The messages say the stay is over — said outright, or via a discharge already made."""
    if fatti.ricovero is Ricovero.CONCLUSO:
        return True
    return (
        fatti.dimissione is not None
        and fatti.dimissione.stato is StatoDimissione.AVVENUTA
    )


def _scheduled_discharge_day(fatti: Fatti, now: datetime) -> date | None:
    """The day a SCHEDULED (not yet done) discharge falls on, if we can trust it."""
    dimissione = fatti.dimissione
    if dimissione is None or dimissione.stato is not StatoDimissione.FISSATA:
        return None
    return _plausible_day(dimissione.quando, now)


def _animal_words(animale: Animale) -> tuple[str, ...]:
    """The parts of an animal we would write into a name, in template order."""
    return tuple(
        word
        for word in (_clean(animale.specie), _clean(animale.nome))
        if _is_plausible_animal_word(word)
    )


def _animal_label(animale: Animale) -> str:
    """How an animal is called out in a motivo: its own name, else its species."""
    nome = _clean(animale.nome)
    if _is_plausible_animal_word(nome):
        return nome
    return _clean(animale.specie)


# --- The tag rules -------------------------------------------------------------


def _tag_proposals(
    entry: ConversationTriage,
    convo: Conversation,
    *,
    ours: frozenset[str],
    now: datetime,
) -> list[Proposal]:
    """Every tag proposal one conversation earns this run.

    ``convo.tags`` is compared byte for byte: a contact carrying ``ricoverato`` in
    lowercase does NOT carry ours, and gets the proposal.
    """
    proposals: list[Proposal] = []
    tags = convo.tags

    def propose(tipo: TipoProposta, payload: dict, motivo: str, matures_at=None) -> None:
        proposals.append(
            Proposal(
                contact_id=entry.contact_id,
                tipo=tipo,
                payload=payload,
                motivo=motivo,
                matures_at=matures_at,
            )
        )

    # "Da gestire subito" reads the JUDGMENT, not the facts. That is why it sits before
    # the early return: a malformed `fatti` costs its own facts (PR1 degrades it to None
    # for that entry alone) and must not also cost a rule that never needed them.
    if entry.gruppo is Gruppo.SUBITO and TAG_DA_GESTIRE_SUBITO not in tags:
        propose(
            TipoProposta.TAG_ADD,
            {"tag": TAG_DA_GESTIRE_SUBITO},
            "il triage l'ha messa fra le cose da gestire subito",
        )

    fatti = entry.fatti
    if fatti is None:
        return proposals

    discharge_day = _scheduled_discharge_day(fatti, now)

    if fatti.ricovero is Ricovero.IN_CORSO and TAG_RICOVERATO not in tags:
        propose(
            TipoProposta.TAG_ADD,
            {"tag": TAG_RICOVERATO},
            "dai messaggi risulta un ricovero in corso",
        )

    # Removal only for a tag WE put there (invariant 1) that the contact still carries.
    # Both halves matter: without the system_tags row we would be stripping a colleague's
    # tag, and without the tag on the contact we would be proposing a no-op PATCH.
    if TAG_RICOVERATO in tags and TAG_RICOVERATO in ours:
        if _discharge_done(fatti):
            propose(
                TipoProposta.TAG_REMOVE,
                {"tag": TAG_RICOVERATO},
                "dai messaggi il ricovero risulta concluso",
            )
        elif discharge_day is not None:
            # The only scheduled removal born inside a run, because a stay ends by what
            # the messages say. If instead the chat goes silent after the discharge was
            # set, no removal is ever proposed and the tag survives until the 14-day net:
            # that is the declared price of "mai a tempo", not an oversight.
            propose(
                TipoProposta.TAG_REMOVE,
                {"tag": TAG_RICOVERATO},
                f"dimissione fissata per il {_it_date(discharge_day)}",
                matures_at=next_morning(discharge_day),
            )

    if (
        discharge_day is not None
        and discharge_day == today_in_clinic(now)
        and TAG_DIMISSIONE_OGGI not in tags
    ):
        propose(
            TipoProposta.TAG_ADD,
            # `quando` travels so the follow-up deadline is computed from the discharge
            # date, not from whenever the tap happens to land.
            {"tag": TAG_DIMISSIONE_OGGI, "quando": discharge_day.isoformat()},
            "la dimissione è fissata per oggi",
        )

    return proposals


# --- The rename rule -----------------------------------------------------------


def _is_poor_name(name: str) -> bool:
    """Deterministically: "this is a WhatsApp default, not a name we can work with".

    Three shapes, all seen live: empty, a single word ("Ale", "Bonucci"), or anything
    carrying digits ("Gabri92", a bare phone number). Nothing here is a judgment about
    the person — it is a judgment about the string.
    """
    if not name:
        return True
    if any(char.isdigit() for char in name):
        return True
    return len(name.split()) < 2


def _rename_proposal(
    entry: ConversationTriage, convo: Conversation
) -> Proposal | None:
    """The rename this conversation earns, or ``None``.

    Two independent triggers, because the second one is about a name that is NOT poor:

    1. the current name is unusable and the facts let us do better;
    2. the current name carries an animal and the facts reveal a second one — then the
       name is SIMPLIFIED to the owner alone (decisione utente: more than one animal,
       the animals live in tags or notes, never in the name).

    The owner is the anchor of the template: with no plausible ``proprietario`` there is
    no proposal at all, because the alternative would be inventing the head of a name.
    """
    fatti = entry.fatti
    if fatti is None:
        return None

    owner = _clean(fatti.proprietario)
    if not _is_plausible_person_name(owner):
        return None

    current = _clean(convo.name)
    # The prompt forbids reading the owner off the contact name (that name is exactly
    # what T10 exists to fix, and the transcript header puts it in front of the model).
    # This is the deterministic net that closes the case instead of trusting it.
    if owner.casefold() == current.casefold():
        return None

    usable = [animale for animale in fatti.animali if _animal_words(animale)]
    if len(usable) == 1:
        proposed = " ".join((owner, *_animal_words(usable[0])))
    else:
        # No animals: the owner is all we know, and a partial improvement is still an
        # improvement. Two or more: the owner is all we WRITE.
        proposed = owner

    if proposed == current:
        return None

    carries_animal = len(usable) >= 2 and any(
        _contains_word(current, _clean(animale.nome))
        for animale in usable
        if _is_plausible_animal_word(_clean(animale.nome))
    )
    if not (_is_poor_name(current) or carries_animal):
        return None

    if carries_animal:
        listed = ", ".join(label for label in map(_animal_label, usable) if label)
        motivo = f"ha più animali: {listed}"
    else:
        motivo = "il nome attuale non è utilizzabile"

    return Proposal(
        contact_id=entry.contact_id,
        tipo=TipoProposta.RENAME,
        payload={"nome": proposed},
        motivo=motivo,
    )


# --- Idempotence ---------------------------------------------------------------


def should_propose(
    proposal: Proposal, decisions: Sequence[Decision], *, now: datetime
) -> bool:
    """Whether this proposal is new enough to be worth asking about.

    An identical PENDING proposal blocks a second one: that is what keeps runs from
    piling duplicates into the queue while nothing is being tapped.

    A refused RENAME is refused forever — a name we got wrong once we would get wrong
    again. A refused TAG comes back after ``TAG_REFUSAL_COOLDOWN``, because a tag that
    was wrong in July can be right in September: the situation changed, the name did not.

    ``eseguita``/``fallita`` do not block. The criterion is the state of the world, and
    the "the tag is not there already" check upstream already covers the executed add.
    """
    relevant = [decision for decision in decisions if decision.key == proposal.key]
    if any(decision.stato == StatoProposta.PENDING.value for decision in relevant):
        return False

    refusals = [
        decision.when
        for decision in relevant
        if decision.stato == StatoProposta.RIFIUTATA.value
    ]
    if not refusals:
        return True
    if proposal.tipo is TipoProposta.RENAME:
        return False

    latest = max((when for when in refusals if when is not None), default=None)
    if latest is None:
        # A refusal we cannot date: we cannot tell whether the cooldown expired, and
        # silence beats nagging about something already turned down.
        return False
    return now - latest >= TAG_REFUSAL_COOLDOWN


# --- Entry points --------------------------------------------------------------


def build_proposals(
    entries: Sequence[ConversationTriage],
    conversations: Sequence[Conversation],
    *,
    system_tags: Mapping[str, frozenset[str]] | None = None,
    decisions: Sequence[Decision] = (),
    now: datetime,
) -> list[Proposal]:
    """Every proposal this run earns, already filtered against what the DB knows.

    ``system_tags`` maps ``contact_id`` to the tags WE applied (invariant 1);
    ``decisions`` are the past proposals for these same contacts. Both come from the
    store; passing them in keeps this function pure and testable with dicts.

    Conversations carry the current tags and the current name, which is why they travel
    alongside the triage entries: the judgment object has neither.
    """
    ours_by_contact = system_tags if system_tags is not None else {}
    by_contact = {convo.contact_id: convo for convo in conversations}
    proposals: list[Proposal] = []
    seen: set[ProposalKey] = set()

    for entry in entries:
        convo = by_contact.get(entry.contact_id)
        if convo is None:
            # Cannot happen: contact_id is filled from the source, never by the model.
            # Skipping costs one conversation's proposals, not the run -- but skipping
            # SILENTLY would make this the only way to reach zero proposals leaving no
            # trace anywhere, and "cannot happen" is exactly when that bill comes due.
            #
            # The contact_id belongs in the line. The ban on it in CLAUDE.md is about
            # TELEMETRY metadata, which leaves this machine and lands in a dashboard;
            # this is journald, where the same id is already written to `proposals` on
            # Supabase anyway. Without it the warning says something is wrong and
            # nothing about which conversation to go and look at.
            logger.warning(
                "Voce di triage senza conversazione corrispondente (contact_id %s); "
                "saltata: nessuna proposta per questa conversazione",
                entry.contact_id,
            )
            continue

        candidates = _tag_proposals(
            entry,
            convo,
            ours=frozenset(ours_by_contact.get(entry.contact_id, ())),
            now=now,
        )
        rename = _rename_proposal(entry, convo)
        if rename is not None:
            candidates.append(rename)

        for proposal in candidates:
            if proposal.key in seen:
                continue
            if not should_propose(proposal, decisions, now=now):
                continue
            seen.add(proposal.key)
            proposals.append(proposal)

    return proposals


def followups_for(proposal: Proposal, *, now: datetime) -> tuple[Proposal, ...]:
    """The calendar removals that exist only because a tag was actually applied.

    Called by the executor AFTER a successful ``tag_add``, never during a run: a
    scheduled removal for a tag that might never be applied would be a row somebody has
    to interpret later, and there is no honest state to give it if the add is ignored.

    ``Ricoverato`` returns nothing, and THAT ABSENCE IS THE RULE: a stay ends when the
    messages say so, never because time passed. A long stay with a silent chat keeps its
    tag, which is the whole reason ``ricovero`` is tri-valued in the first place.
    """
    if proposal.tipo is not TipoProposta.TAG_ADD:
        return ()

    tag = proposal.tag
    if tag == TAG_DIMISSIONE_OGGI:
        day = _parse_day(proposal.payload.get("quando")) or today_in_clinic(now)
        return (
            Proposal(
                contact_id=proposal.contact_id,
                tipo=TipoProposta.TAG_REMOVE,
                payload={"tag": tag},
                motivo="la dimissione era di ieri: il tag ha esaurito la sua giornata",
                matures_at=next_morning(day),
            ),
        )
    if tag == TAG_DA_GESTIRE_SUBITO:
        return (
            Proposal(
                contact_id=proposal.contact_id,
                tipo=TipoProposta.TAG_REMOVE,
                payload={"tag": tag},
                motivo="sono passate 48 ore senza che tornasse urgente",
                matures_at=now + SUBITO_TTL,
            ),
        )
    return ()
