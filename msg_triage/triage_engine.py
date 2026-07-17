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
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .config import Config
from .source_adapter import Conversation

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"  # while tuning the prompt; consider Sonnet once stable
DEFAULT_MAX_TOKENS = 16000  # covers thinking + output, under the non-streaming timeout
DEFAULT_EFFORT = "high"  # API default; drop to "medium" if runs get slow/costly

# The canonical operative prompt lives in the docs (single source of truth).
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "docs" / "triage_system_prompt.md"
_PROMPT_START = "## SYSTEM PROMPT (testo da usare)"
_PROMPT_END = "## Note per lo sviluppatore"


class TriageError(RuntimeError):
    """Raised when the triage call fails or returns unusable output."""


# --- Domain output types (frozen, like the neutral format) ---------------------


class Gruppo(str, Enum):
    """Which of the three triage buckets a conversation lands in."""

    SUBITO = "subito"
    IN_CORSO = "in_corso"
    RUMORE = "rumore"


class Urgenza(str, Enum):
    """Clinical urgency, read through the exotics/avian filter."""

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


@dataclass(frozen=True)
class ConversationTriage:
    """The structured judgment for one conversation.

    ``contact_id`` and ``nome`` come from the source conversation (not the model).
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


@dataclass(frozen=True)
class TriageResult:
    """The whole triage: one entry per conversation the model returned."""

    conversations: tuple[ConversationTriage, ...]


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


def build_output_schema() -> dict:
    """The strict JSON schema the model must fill.

    Judgment fields only: ``contact_id``/``nome`` are NOT requested (the engine
    fills them from the source via ``ref``). Strict-output rules: every object has
    ``additionalProperties: false`` and lists all fields as ``required``; the
    optional promise is expressed with ``anyOf`` + ``null``.
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
            "motivo": {"type": "string", "description": "Perché è in questo gruppo, in breve."},
            "urgenza": {"type": "string", "enum": _URGENZA_VALUES, "description": "Urgenza clinica (filtro esotici/aviari)."},
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
    return {
        "type": "object",
        "properties": {"conversazioni": {"type": "array", "items": item}},
        "required": ["conversazioni"],
        "additionalProperties": False,
    }


# --- Response validation: model JSON -> TriageResult ----------------------------


def _build_conversation_triage(item: dict, convo: Conversation) -> ConversationTriage:
    """Build one domain entry from a model item + its source conversation.

    Enum coercion / missing keys raise, so the caller can drop a single bad entry
    without failing the whole run.
    """
    promessa_raw = item.get("promessa_rilevata")
    promessa = (
        Promessa(testo=promessa_raw["testo"], scadenza_stimata=promessa_raw["scadenza_stimata"])
        if promessa_raw
        else None
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
    """

    def __init__(
        self,
        client,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = DEFAULT_EFFORT,
        now=None,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._effort = effort
        self._now = now if now is not None else (lambda: datetime.now(timezone.utc))

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
                "format": {"type": "json_schema", "schema": build_output_schema()},
                "effort": self._effort,
            },
            messages=[{"role": "user", "content": user_message}],
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
        return "\n".join(parts)

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
) -> TriageEngine:
    """Wire a :class:`TriageEngine` from validated :class:`Config`.

    The API key comes from ``config.anthropic_api_key`` (never read from the
    environment directly here). ``client`` is injectable for tests; when omitted a
    real ``anthropic.Anthropic`` client is created (imported lazily so the engine
    and its tests stay import-light).
    """
    if client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    return TriageEngine(client, model=model, max_tokens=max_tokens, effort=effort)
