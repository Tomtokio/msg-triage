"""Triage engine: neutral conversations -> one structured Claude call -> triage.

This is the "brain". It consumes ONLY the neutral :class:`Conversation` format
(never anything Callbell-specific) and returns a single structured object with
exactly one LLM call; the three renderers (voice/schema/table, T5) derive from
that object. Design choices that make it robust and portable:

- The operative system prompt is the single source of truth in
  ``docs/triage_system_prompt.md`` (tune it there); it already defines the
  judgment (double role, proportional detail, ethical boundary). We only add the
  output-structure spec, as its own developer notes instruct.
- Determinism around inference: a hand-written strict JSON schema constrains the
  model, and we validate its output in code.
- ``ref`` indirection: conversations are numbered ``[1..N]`` and the model returns
  that index, not the opaque ``contact_id``. The engine maps ``ref`` back to the
  real ``contact_id``/``name`` from OUR data, so the model can never hallucinate
  an identifier or misspell a name.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from . import telemetry
from .config import Config
from .source_adapter import Conversation

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"  # while tuning the prompt; consider Sonnet once stable
DEFAULT_MAX_TOKENS = 16000  # covers thinking + output, under the non-streaming timeout
DEFAULT_EFFORT = "high"  # API default; drop to "medium" if runs get slow/costly

# Business label for the one LLM call, in the telemetry usage vocabulary (CLAUDE.md).
TRIAGE_OPERATION = "conversation_triage"

# The canonical operative prompt lives in the docs (single source of truth).
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "docs" / "triage_system_prompt.md"
_PROMPT_START = "## SYSTEM PROMPT (testo da usare)"
_PROMPT_END = "## Note per lo sviluppatore"

# The T10 facts block lives in the same doc, in its own section AFTER the developer
# notes -- anywhere before them and it would be swallowed by load_triage_system().
_FACTS_START = "## BLOCCO FATTI DI STATO (testo da usare — T10)"
_FACTS_END = "## Note sul blocco fatti (NON parte del prompt)"
# Substituted with today's date in the clinic's timezone; see load_facts_block().
_FACTS_DATE_PLACEHOLDER = "{oggi}"
_CLINIC_TZ = "Europe/Rome"


class TriageError(RuntimeError):
    """Raised when the triage call fails or returns unusable output."""


# --- Domain output types (frozen, like the neutral format) ---------------------


class Gruppo(str, Enum):
    """Which of the three triage buckets a conversation lands in."""

    SUBITO = "subito"
    IN_CORSO = "in_corso"
    RUMORE = "rumore"


class Urgenza(str, Enum):
    """How soon it must be handled — a time window, not clinical severity."""

    EMERGENZA = "emergenza"
    ALTA = "alta"
    MEDIA = "media"
    BASSA = "bassa"


class Presidio(str, Enum):
    """Whether someone is currently on the conversation (not whether it's solved)."""

    PRESIDIATA = "presidiata"
    SCOPERTA = "scoperta"


class Temperatura(str, Enum):
    """Emotional temperature of the owner (frustration/worry), independent of urgency."""

    ALTA = "alta"
    MEDIA = "media"
    BASSA = "bassa"


@dataclass(frozen=True)
class Promessa:
    """An explicit promise with a recognizable deadline (used conservatively)."""

    testo: str
    scadenza_stimata: str


# --- State facts (T10, behind ENABLE_PROPOSALS) --------------------------------
#
# These are NOT judgment. They are a mechanical extract of what the messages already
# say, which PR2 turns into typed proposals with deterministic rules. Everything
# here is tri-valued on purpose, because "the messages don't mention it" has to stay
# distinguishable from a negative answer: a proposal built on that confusion would
# strip a tag off a conversation that simply went quiet.


class Ricovero(str, Enum):
    """Whether the animal is currently admitted, as stated in the messages.

    Three values, not a bool. With a bool, ``False`` would mean both "discharged"
    and "nobody mentioned it in this window", and removing the ``Ricoverato`` tag on
    the second is removal-by-elapsed-time -- exactly what T10 forbids, because a long
    stay with a silent chat must not lose its tag.
    """

    IN_CORSO = "in_corso"
    CONCLUSO = "concluso"
    NON_MENZIONATO = "non_menzionato"


class StatoDimissione(str, Enum):
    """Scheduled or already done. "Fissata" is the prompt's own word for it."""

    FISSATA = "fissata"
    AVVENUTA = "avvenuta"


@dataclass(frozen=True)
class Animale:
    """One animal named in the messages; either field may be unknown."""

    specie: str | None
    nome: str | None


@dataclass(frozen=True)
class Dimissione:
    """A discharge the messages talk about. ``None`` upstream means they don't.

    ``quando`` is an ISO ``YYYY-MM-DD`` string, already validated, or ``None``.
    A string and not a ``date`` because it travels inside a JSON body to PostgREST.
    """

    stato: StatoDimissione
    quando: str | None


@dataclass(frozen=True)
class Fatti:
    """What the messages state about a conversation, with nothing inferred."""

    ricovero: Ricovero
    dimissione: Dimissione | None
    animali: tuple[Animale, ...]
    proprietario: str | None


@dataclass(frozen=True)
class ConversationTriage:
    """The structured judgment for one conversation.

    ``contact_id`` and ``nome`` come from the source conversation (not the model).
    ``fatti`` is ``None`` in exactly two cases: the flag is off, or the model's facts
    for this entry were malformed and got dropped on their own (see
    ``_build_conversation_triage``).
    """

    contact_id: str
    nome: str
    gruppo: Gruppo
    motivo: str
    urgenza: Urgenza
    presidio: Presidio
    temperatura: Temperatura
    stato_sintetico: str
    azione_suggerita: str
    promessa_rilevata: Promessa | None
    fatti: Fatti | None = None


@dataclass(frozen=True)
class TriageResult:
    """The whole triage: one entry per conversation the model returned."""

    conversations: tuple[ConversationTriage, ...]


# --- Species marker (part of what the model emits) -----------------------------
#
# The prompt tells the model to wrap the animal's species in double asterisks every
# time it names it ("la **tartaruga** Ruga" — docs/triage_system_prompt.md, "Come
# scrivere"). It is a rendering hint first (renderers turn it into ``<i>``), but it
# is also the only place the species appears in a machine-readable form, so T7
# persistence reads it from here instead of asking the model for one more field.
# Only balanced pairs match: a lone marker stays literal and can never open an
# ``<i>`` that isn't closed.

SPECIES_MARKER = re.compile(r"\*\*(.+?)\*\*")

# Beyond this many characters the marked text is a whole clause, not a species: the
# model broke the "marca solo la specie" rule and we would store a sentence in a
# column meant for one word. NULL is the better answer.
_MAX_SPECIES_LEN = 40


def extract_species(entry: ConversationTriage) -> str | None:
    """The species named in a triage entry, or ``None``.

    Reads the first ``**...**`` marker across ``motivo`` (the shortest, most factual
    field), then ``stato_sintetico``, then ``azione_suggerita``.

    ``None`` IS THE EXPECTED OUTCOME for a good share of the entries, and it is not a
    defect of this function: the model marks the species when it names it ("la
    **tartaruga** Bianca") and it does not always name it ("la dimissione del
    coniglio"). The fallback chain is the mitigation; if the hit rate ever looks too
    low, the cure is to reinforce the rule in the prompt, not to loosen the parsing
    here — a looser regex would only produce wrong species, which is worse than none.
    """
    for field in (entry.motivo, entry.stato_sintetico, entry.azione_suggerita):
        match = SPECIES_MARKER.search(field)
        if match is None:
            continue
        species = " ".join(match.group(1).split())
        if species and len(species) <= _MAX_SPECIES_LEN:
            return species
    return None


# --- System prompt loading (single source of truth in docs/) -------------------


def load_triage_system(path: Path | None = None) -> str:
    """Load the operative triage system prompt from ``docs/triage_system_prompt.md``.

    Extracts the section between the "SYSTEM PROMPT" heading and the developer
    notes, so the doc stays the single source of truth. Raises :class:`TriageError`
    if the markers are missing or the section is empty.
    """
    prompt_path = path if path is not None else _PROMPT_PATH
    text = prompt_path.read_text(encoding="utf-8")
    if _PROMPT_START not in text:
        raise TriageError(f"Prompt start marker not found in {prompt_path}")
    operative = text.split(_PROMPT_START, 1)[1].split(_PROMPT_END, 1)[0].strip()
    if operative.endswith("---"):  # drop the trailing horizontal-rule separator
        operative = operative[:-3].rstrip()
    if not operative:
        raise TriageError(f"Operative prompt section is empty in {prompt_path}")
    return operative


TRIAGE_SYSTEM = load_triage_system()


def load_facts_block(path: Path | None = None) -> str:
    """Load the T10 facts-extraction block from ``docs/triage_system_prompt.md``.

    Twin of :func:`load_triage_system`, with two differences worth knowing.

    It is read LAZILY -- only when ``extract_facts`` is on -- so a malformed section
    can never keep the bot from booting with the feature off. Same reasoning that
    keeps ``ENABLE_PROPOSALS`` out of ``REQUIRED_ENV_VARS``: a feature must not be
    able to prevent a start. With the flag on it fails loudly instead, at engine
    construction: ``ENABLE_PROPOSALS`` is opt-in, and a silent fallback to "off"
    would make T10 mysteriously dead.

    And it is re-read on EVERY run, because ``build_triage_engine`` is called once
    per ``/triage``, while ``TRIAGE_SYSTEM`` is frozen at import. While tuning: edits
    to this block take effect immediately, edits to the system prompt only after a
    restart. That asymmetry has cost an evening before.
    """
    prompt_path = path if path is not None else _PROMPT_PATH
    text = prompt_path.read_text(encoding="utf-8")
    if _FACTS_START not in text:
        raise TriageError(f"Facts block start marker not found in {prompt_path}")
    block = text.split(_FACTS_START, 1)[1].split(_FACTS_END, 1)[0].strip()
    if block.endswith("---"):  # drop the trailing horizontal-rule separator
        block = block[:-3].rstrip()
    if not block:
        raise TriageError(f"Facts block section is empty in {prompt_path}")
    return block


# --- Serialization: neutral conversations -> transcript ------------------------


def _format_timestamp(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def serialize_conversations(
    conversations: list[Conversation],
) -> tuple[str, dict[int, Conversation]]:
    """Render neutral conversations into a compact transcript for the model.

    Each conversation gets a stable index ``[1..N]`` used as the ``ref`` the model
    returns; the returned map lets the engine recover the real ``contact_id``/
    ``name`` deterministically. Empty-text messages are skipped. Nothing
    Callbell-specific appears here.
    """
    ref_map: dict[int, Conversation] = {}
    blocks: list[str] = []
    for index, convo in enumerate(conversations, start=1):
        ref_map[index] = convo
        header = [f"[{index}] {convo.name or 'Senza nome'}"]
        if convo.channel:
            header.append(f"canale: {convo.channel}")
        header.append(
            f"presidio: assegnata a {convo.assigned_user}"
            if convo.assigned_user
            else "presidio: non assegnata"
        )
        if convo.tags:
            header.append("tag: " + ", ".join(convo.tags))
        lines = [" — ".join(header)]
        for message in convo.messages:
            if not message.text.strip():
                continue
            lines.append(
                f"  [{_format_timestamp(message.timestamp)}] "
                f"{message.role.value}: {message.text}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks), ref_map


# --- Output schema (strict; judgment fields only) ------------------------------

_GRUPPO_VALUES = [g.value for g in Gruppo]
_URGENZA_VALUES = [u.value for u in Urgenza]
_PRESIDIO_VALUES = [p.value for p in Presidio]
_TEMPERATURA_VALUES = [t.value for t in Temperatura]
_RICOVERO_VALUES = [r.value for r in Ricovero]
_STATO_DIMISSIONE_VALUES = [s.value for s in StatoDimissione]

_NULLABLE_STRING = [{"type": "string"}, {"type": "null"}]


def _facts_schema() -> dict:
    """The ``fatti`` property, added to each item only when the flag is on.

    The descriptions here are deliberately MECHANICAL, and carry none of the
    judgment vocabulary (urgente, grave, subito, priorità, preoccupato). Three of
    the four prompt regressions this project has lived through name a field
    description as a cause: a description that argues, in the same request as a
    prompt that argues the opposite, is how the judgment drifts.
    """
    animale = {
        "type": "object",
        "properties": {
            "specie": {
                "anyOf": _NULLABLE_STRING,
                "description": "La specie dell'animale come compare nei messaggi, oppure null.",
            },
            "nome": {
                "anyOf": _NULLABLE_STRING,
                "description": "Il nome proprio dell'animale, oppure null.",
            },
        },
        "required": ["specie", "nome"],
        "additionalProperties": False,
    }
    dimissione = {
        "type": "object",
        "properties": {
            "stato": {
                "type": "string",
                "enum": _STATO_DIMISSIONE_VALUES,
                "description": "fissata se la dimissione è programmata, avvenuta se è già stata fatta.",
            },
            "quando": {
                "anyOf": _NULLABLE_STRING,
                "description": "La data in formato AAAA-MM-GG, oppure null se non ricavabile dai messaggi.",
            },
        },
        "required": ["stato", "quando"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "ricovero": {
                "type": "string",
                "enum": _RICOVERO_VALUES,
                "description": "in_corso se i messaggi dicono che l'animale è ricoverato, concluso se dicono che il ricovero è finito, non_menzionato se non ne parlano.",
            },
            "dimissione": {
                "anyOf": [dimissione, {"type": "null"}],
                "description": "La dimissione se i messaggi ne parlano, altrimenti null.",
            },
            "animali": {
                "type": "array",
                "items": animale,
                "description": "Gli animali nominati nei messaggi. Lista vuota se non ce ne sono.",
            },
            "proprietario": {
                "anyOf": _NULLABLE_STRING,
                "description": "Il nome del proprietario solo se il cliente si firma o si nomina scrivendo, altrimenti null. Il nome del contatto non conta come fonte.",
            },
        },
        "required": ["ricovero", "dimissione", "animali", "proprietario"],
        "additionalProperties": False,
        "description": "Quello che i messaggi dichiarano, estratto senza dedurre. Non entra nel giudizio.",
    }


def build_output_schema(*, include_facts: bool = False) -> dict:
    """The strict JSON schema the model must fill.

    Judgment fields only: ``contact_id``/``nome`` are NOT requested (the engine
    fills them from the source via ``ref``). Strict-output rules: every object has
    ``additionalProperties: false`` and lists all fields as ``required``; the
    optional promise is expressed with ``anyOf`` + ``null``.

    ``include_facts`` (T10) APPENDS ``fatti`` after the existing properties and
    after the existing ``required`` entries; it never rebuilds or reorders them.
    That is not a style preference: with the flag off the schema has to be the
    object of before, key order included, because insertion order is what the SDK
    serializes and it is also prompt order -- moving ``fatti`` above ``motivo``
    would change the sequence in which the model fills the fields.
    """
    promessa = {
        "type": "object",
        "properties": {
            "testo": {"type": "string", "description": "La promessa fatta al cliente, testuale."},
            "scadenza_stimata": {
                "type": "string",
                "description": "Quando era attesa la risposta (es. '2026-07-17 14:00' o 'entro sera').",
            },
        },
        "required": ["testo", "scadenza_stimata"],
        "additionalProperties": False,
    }
    item = {
        "type": "object",
        "properties": {
            "ref": {"type": "integer", "description": "Il numero [n] della conversazione in input."},
            "gruppo": {"type": "string", "enum": _GRUPPO_VALUES, "description": "Il gruppo di triage."},
            "motivo": {"type": "string", "description": "Cosa è successo, in una riga: il fatto concreto (animale + fatto), non la categoria. Senza il nome del cliente né titoli — lo mette l'interfaccia — e senza stile etichetta coi due punti: una frase. Formula fissa e identica quando non c'è niente da raccontare (conversazione vuota/chiusa, orari)."},
            "urgenza": {"type": "string", "enum": _URGENZA_VALUES, "description": "Entro quando va gestita: emergenza (minuti) / alta (poche ore) / media (in giornata) / bassa (può aspettare domani)."},
            "presidio": {"type": "string", "enum": _PRESIDIO_VALUES, "description": "presidiata se qualcuno la sta gestendo, altrimenti scoperta."},
            "temperatura": {"type": "string", "enum": _TEMPERATURA_VALUES, "description": "Temperatura emotiva del proprietario."},
            "stato_sintetico": {
                "type": "string",
                "description": "Micro-storia dello stato: chi ha chiesto cosa, a che punto è. Lunghezza PROPORZIONALE alla temperatura (routine = mezza riga; calda/delicata = due-tre righe).",
            },
            "azione_suggerita": {"type": "string", "description": "Cosa dovrebbe fare il responsabile, o '' se nulla."},
            "promessa_rilevata": {
                "anyOf": [promessa, {"type": "null"}],
                "description": "Promessa esplicita con scadenza riconoscibile, oppure null. Conservativo: nel dubbio, null.",
            },
        },
        "required": [
            "ref",
            "gruppo",
            "motivo",
            "urgenza",
            "presidio",
            "temperatura",
            "stato_sintetico",
            "azione_suggerita",
            "promessa_rilevata",
        ],
        "additionalProperties": False,
    }
    if include_facts:
        item["properties"]["fatti"] = _facts_schema()
        item["required"] = item["required"] + ["fatti"]
    return {
        "type": "object",
        "properties": {"conversazioni": {"type": "array", "items": item}},
        "required": ["conversazioni"],
        "additionalProperties": False,
    }


# --- Response validation: model JSON -> TriageResult ----------------------------


def _iso_date_or_none(value) -> str | None:
    """``quando`` only when it really is an ISO date; never a guess.

    PR2 does arithmetic on this field ("+ 24h", "same day"), so a half-understood
    date is worse than no date at all: it would mature a proposal on the wrong day
    and nobody would notice. Anything that is not ``YYYY-MM-DD`` becomes ``None``,
    and the discharge keeps its ``stato`` -- knowing that a discharge is scheduled
    is still worth something without knowing when.
    """
    if not isinstance(value, str):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def _build_fatti(raw: dict) -> Fatti:
    """Build the facts of one entry. Raises on anything malformed; see the caller."""
    dimissione_raw = raw.get("dimissione")
    dimissione = (
        Dimissione(
            stato=StatoDimissione(dimissione_raw["stato"]),
            quando=_iso_date_or_none(dimissione_raw.get("quando")),
        )
        if dimissione_raw
        else None
    )
    animali = tuple(
        Animale(specie=animale.get("specie"), nome=animale.get("nome"))
        for animale in raw.get("animali") or ()
    )
    return Fatti(
        ricovero=Ricovero(raw["ricovero"]),
        dimissione=dimissione,
        animali=animali,
        proprietario=raw.get("proprietario"),
    )


def _build_conversation_triage(item: dict, convo: Conversation) -> ConversationTriage:
    """Build one domain entry from a model item + its source conversation.

    Enum coercion / missing keys raise, so the caller can drop a single bad entry
    without failing the whole run. The facts are the exception: they are accessory
    and are caught HERE, so that a malformed ``fatti`` costs its own facts and never
    the triage line -- the triage is the product, the facts are the side dish.
    """
    promessa_raw = item.get("promessa_rilevata")
    promessa = (
        Promessa(testo=promessa_raw["testo"], scadenza_stimata=promessa_raw["scadenza_stimata"])
        if promessa_raw
        else None
    )
    fatti_raw = item.get("fatti")
    fatti = None
    if fatti_raw is not None:
        try:
            fatti = _build_fatti(fatti_raw)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            # The exception CLASS only, never the payload: facts carry owners' and
            # animals' names, and this line goes to journald.
            logger.warning(
                "Fatti malformati per ref %r (%s); ignorati",
                item.get("ref"),
                type(exc).__name__,
            )
    return ConversationTriage(
        contact_id=convo.contact_id,  # from source, never the model
        nome=convo.name,  # from source, never the model
        gruppo=Gruppo(item["gruppo"]),
        motivo=item["motivo"],
        urgenza=Urgenza(item["urgenza"]),
        presidio=Presidio(item["presidio"]),
        temperatura=Temperatura(item["temperatura"]),
        stato_sintetico=item["stato_sintetico"],
        azione_suggerita=item["azione_suggerita"],
        promessa_rilevata=promessa,
        fatti=fatti,
    )


def parse_triage_response(data: dict, ref_map: dict[int, Conversation]) -> TriageResult:
    """Validate the model's JSON and map each entry back to its conversation.

    Unknown/duplicate/malformed entries are dropped with a warning; omitted
    conversations are logged. Raises :class:`TriageError` only if nothing usable
    came back for a non-empty input.
    """
    items = data.get("conversazioni")
    if not isinstance(items, list):
        raise TriageError("Triage response missing a 'conversazioni' list")

    triaged: list[ConversationTriage] = []
    seen: set[int] = set()
    for item in items:
        ref = item.get("ref") if isinstance(item, dict) else None
        convo = ref_map.get(ref)
        if convo is None:
            logger.warning("Triage returned unknown ref %r; dropping entry", ref)
            continue
        if ref in seen:
            logger.warning("Triage returned duplicate ref %r; dropping extra", ref)
            continue
        try:
            entry = _build_conversation_triage(item, convo)
        except (KeyError, ValueError) as exc:
            logger.warning("Triage entry for ref %r malformed (%s); dropping", ref, exc)
            continue
        seen.add(ref)
        triaged.append(entry)

    missing = [ref for ref in ref_map if ref not in seen]
    if missing:
        logger.warning(
            "Triage omitted %d/%d conversations (refs %s)", len(missing), len(ref_map), missing
        )
    if ref_map and not triaged:
        raise TriageError("Triage returned no usable entries for a non-empty input")
    return TriageResult(conversations=tuple(triaged))


def _extract_json_text(response) -> str:
    """Return the JSON text block from a Messages API response.

    With structured outputs the answer is a single text block; thinking blocks may
    precede it. Raises :class:`TriageError` if no text block is present.
    """
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise TriageError("Triage response contained no text block")


# --- Engine --------------------------------------------------------------------


class TriageEngine:
    """Turns neutral conversations into one structured triage via a single call.

    ``client`` (an ``anthropic.Anthropic`` or any object exposing
    ``messages.create``) and ``now`` are injected so the engine is unit-testable
    with no real network. One ``messages.create`` call per triage.

    ``extract_facts`` (T10) is off by default and, when off, this class must behave
    exactly as it did before it existed: same system prompt, same schema, same user
    message, byte for byte. The facts block is loaded here at construction so a
    broken doc surfaces at startup rather than mid-triage.
    """

    def __init__(
        self,
        client,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = DEFAULT_EFFORT,
        now=None,
        extract_facts: bool = False,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._effort = effort
        self._now = now if now is not None else (lambda: datetime.now(timezone.utc))
        self._extract_facts = extract_facts
        self._facts_block = load_facts_block() if extract_facts else None

    def triage(
        self,
        conversations: list[Conversation],
        *,
        previous_state: str | None = None,
    ) -> TriageResult:
        """Triage ``conversations`` into a structured :class:`TriageResult`.

        Empty input short-circuits with no API call. ``previous_state`` (optional)
        is the memory hook for T4: if provided it is injected into the user
        message; T3 does not build or fetch it.
        """
        if not conversations:
            return TriageResult(conversations=())

        transcript, ref_map = serialize_conversations(conversations)
        user_message = self._build_user_message(transcript, previous_state)

        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=TRIAGE_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": build_output_schema(include_facts=self._extract_facts),
                },
                "effort": self._effort,
            },
            messages=[{"role": "user", "content": user_message}],
        )

        # Before validation on purpose: the tokens are spent even when the answer is
        # a refusal or gets truncated, and that cost has to be recorded anyway.
        # getattr all the way down: injected fake clients have no usage on their response.
        api_usage = getattr(response, "usage", None)
        telemetry.usage(
            model=self._model,
            operation=TRIAGE_OPERATION,
            input_tokens=getattr(api_usage, "input_tokens", None),
            output_tokens=getattr(api_usage, "output_tokens", None),
        )

        self._check_stop_reason(response)
        raw = _extract_json_text(response)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TriageError(f"Triage response was not valid JSON: {exc}") from exc

        result = parse_triage_response(data, ref_map)
        logger.info(
            "Triaged %d conversations into %d entries",
            len(conversations),
            len(result.conversations),
        )
        return result

    def _build_user_message(self, transcript: str, previous_state: str | None) -> str:
        now_label = _format_timestamp(self._now())
        parts = [
            "Fai il triage delle conversazioni WhatsApp qui sotto.",
            f"Ora corrente di riferimento: {now_label}.",
            "Per ogni conversazione restituisci un oggetto che usa il suo numero "
            "[n] come campo `ref`. Non inventare né riecheggiare identificativi: "
            "basta il numero.",
        ]
        if previous_state:
            parts.append("\n## Stato del run precedente\n" + previous_state)
        parts.append("\n## Conversazioni\n" + transcript)
        # LAST, after the transcript, and only when on: a facts block placed before
        # the conversations becomes the lens the model reads every message through,
        # which is how the "Urgenza clinica" description bent the whole judgment in
        # the second tuning. Nothing is appended when off -- not even an empty
        # string, which the join would turn into a stray newline.
        if self._facts_block is not None:
            parts.append("\n## Fatti di stato\n" + self._resolved_facts_block())
        return "\n".join(parts)

    def _resolved_facts_block(self) -> str:
        """The facts block with today's date filled in, in the CLINIC's timezone.

        The user message already carries the reference time, but in UTC: around
        midnight "oggi" and "domani" would slide by a day, and ``Dimissione oggi``
        is precisely a same-day rule. The local date lives here, inside the
        flag-gated block, so the shared part of the message stays untouched.
        """
        today = self._now().astimezone(ZoneInfo(_CLINIC_TZ)).strftime("%Y-%m-%d")
        return self._facts_block.replace(_FACTS_DATE_PLACEHOLDER, today)

    @staticmethod
    def _check_stop_reason(response) -> None:
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            raise TriageError("Triage model refused the request (stop_reason=refusal)")
        if stop == "max_tokens":
            raise TriageError(
                "Triage output was truncated (stop_reason=max_tokens); "
                "lower effort or raise max_tokens / switch to streaming"
            )


def build_triage_engine(
    config: Config,
    *,
    client=None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    effort: str = DEFAULT_EFFORT,
    extract_facts: bool | None = None,
) -> TriageEngine:
    """Wire a :class:`TriageEngine` from validated :class:`Config`.

    The API key comes from ``config.anthropic_api_key`` (never read from the
    environment directly here). ``client`` is injectable for tests; when omitted a
    real ``anthropic.Anthropic`` client is created (imported lazily so the engine
    and its tests stay import-light).

    ``extract_facts`` defaults to ``config.enable_proposals``. The explicit override
    exists for one reason: running the A/B on real data (facts off, then on, same
    conversations) without editing ``.env`` between the two runs.
    """
    if client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    if extract_facts is None:
        extract_facts = config.enable_proposals
    return TriageEngine(
        client,
        model=model,
        max_tokens=max_tokens,
        effort=effort,
        extract_facts=extract_facts,
    )
