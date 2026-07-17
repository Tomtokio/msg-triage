"""Renderers: one structured :class:`TriageResult` -> three text outputs.

Design principle (dev_notes n.2): the triage produces ONE structured object with a
single LLM call; here we derive THREE depths of rendering from it, deterministically
and with no further inference. The narrative detail is already baked into
``stato_sintetico`` by the model (the prompt doses length by temperature), so these
renderers only lay it out — they never re-summarize prose.

- ``render_schema`` = giornale di bordo completo, prosa a tre livelli.
- ``render_table``  = una riga compatta per conversazione (testo semplice; niente
                      tabelle monospace, fragili su Telegram mobile).
- ``render_voice``  = sintetico, pensato per la sintesi vocale: apre dall'urgenza,
                      narra solo gli item "da gestire subito" (dal campo breve
                      ``motivo``, non dal paragrafo ``stato_sintetico``) e riassume
                      il resto in una frase di panoramica.

Output is plain text (no Markdown / parse_mode), so the bot needs no escaping. The
three strings map 1:1 to T7's ``triage_runs`` columns via :class:`RenderedTriage`.

Memory (T4) seam: memory signals do not exist on the triage object yet. The single
insertion point is :func:`_memory_clause` (returns ``""`` today); ``render_schema``
already splices it, and the table/voice insertion points are marked. When T4 lands
it will compose the Italian delta phrase there. See ``docs/tasks.md`` T4.
"""

from __future__ import annotations

from dataclasses import dataclass

from .triage_engine import (
    ConversationTriage,
    Gruppo,
    Presidio,
    Temperatura,
    TriageResult,
    Urgenza,
)

# --- Constants (headers match docs/triage_system_prompt.md) --------------------

_H_SUBITO = "DA GESTIRE SUBITO"
_H_IN_CORSO = "IN CORSO"
_H_RUMORE = "RUMORE DI FONDO"
_EMPTY_GROUP = "Nessuna, per ora."

# Whole-triage-empty lines (T3 short-circuits an empty window to no entries).
_EMPTY_SCHEMA = "Nessuna conversazione con attività recente."
_EMPTY_TABLE = _EMPTY_SCHEMA
_EMPTY_VOICE = "Tutto tranquillo: nessuna conversazione recente da segnalare."

_TABLE_STATE_LIMIT = 80  # chars of stato_sintetico kept in a compact table row


# --- Ordering (result.conversations is in model order, not grouped/sorted) -----

_URGENZA_RANK = {
    Urgenza.EMERGENZA: 0,
    Urgenza.ALTA: 1,
    Urgenza.MEDIA: 2,
    Urgenza.BASSA: 3,
}
_PRESIDIO_RANK = {Presidio.SCOPERTA: 0, Presidio.PRESIDIATA: 1}  # uncovered surfaces first
_TEMPERATURA_RANK = {Temperatura.ALTA: 0, Temperatura.MEDIA: 1, Temperatura.BASSA: 2}

_COUNT_WORDS = {
    1: "una",
    2: "due",
    3: "tre",
    4: "quattro",
    5: "cinque",
    6: "sei",
    7: "sette",
    8: "otto",
    9: "nove",
}


def _sort_key(entry: ConversationTriage) -> tuple[int, int, int]:
    return (
        _URGENZA_RANK[entry.urgenza],
        _PRESIDIO_RANK[entry.presidio],
        _TEMPERATURA_RANK[entry.temperatura],
    )


def _bucket(
    result: TriageResult,
) -> tuple[list[ConversationTriage], list[ConversationTriage], list[ConversationTriage]]:
    """Split into ``(subito, in_corso, rumore)``.

    ``subito``/``in_corso`` are sorted by ``(urgenza, presidio, temperatura)``;
    Python's stable sort preserves model order for equal keys. ``rumore`` keeps
    model order (it collapses to one line, so order is irrelevant).
    """
    subito = [e for e in result.conversations if e.gruppo is Gruppo.SUBITO]
    in_corso = [e for e in result.conversations if e.gruppo is Gruppo.IN_CORSO]
    rumore = [e for e in result.conversations if e.gruppo is Gruppo.RUMORE]
    subito.sort(key=_sort_key)
    in_corso.sort(key=_sort_key)
    return subito, in_corso, rumore


def _count_word(n: int) -> str:
    """Small Italian count word for the spoken panoramica (1-9 -> word, else digit)."""
    return _COUNT_WORDS.get(n, str(n))


def _one_line(text: str, limit: int = _TABLE_STATE_LIMIT) -> str:
    """Collapse whitespace/newlines to single spaces and truncate on a word
    boundary with an ellipsis. Used by the compact table only."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    truncated = collapsed[:limit].rsplit(" ", 1)[0]
    return f"{truncated}…"


def _as_sentence(text: str) -> str:
    """Trim and ensure the text ends with sentence punctuation (no double period)."""
    text = text.strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _memory_clause(entry: ConversationTriage) -> str:
    """SEAM T4 (memoria): returns ``""`` today.

    Memory deltas (nuova / ancora scoperta / aspetta da N run / promessa non
    mantenuta) do not exist on the triage object yet — T4 will define and produce
    them. When it does, compose the Italian phrase here from whatever memory source
    T4 establishes; :func:`_schema_paragraph` already appends it, and the
    table/voice insertion points are marked with ``SEAM T4`` comments. ``entry`` is
    accepted now so the call sites are already wired. See ``docs/tasks.md`` T4.
    """
    return ""


# --- SCHEMA: three-level prose, complete giornale di bordo ---------------------


def render_schema(result: TriageResult) -> str:
    """Three-level prose. One paragraph per conversation in SUBITO/IN CORSO;
    RUMORE collapses to a single cumulative line. Opens with the most urgent group.
    """
    if not result.conversations:
        return _EMPTY_SCHEMA
    subito, in_corso, rumore = _bucket(result)
    sections = [
        _schema_section(_H_SUBITO, subito),
        _schema_section(_H_IN_CORSO, in_corso),
        _schema_rumore_section(rumore),
    ]
    return "\n\n".join(sections)


def _schema_section(header: str, entries: list[ConversationTriage]) -> str:
    if not entries:
        return f"{header}\n{_EMPTY_GROUP}"
    lines = [header]
    lines.extend(_schema_paragraph(entry) for entry in entries)
    return "\n".join(lines)


def _schema_paragraph(entry: ConversationTriage) -> str:
    parts = [entry.stato_sintetico.strip()]
    clause = _memory_clause(entry)  # SEAM T4: "" today
    if clause:
        parts.append(clause)
    action = _as_sentence(entry.azione_suggerita)  # azione may already end with "."
    if action:
        parts.append(f"Da fare: {action}")
    return " ".join(parts)


def _schema_rumore_section(entries: list[ConversationTriage]) -> str:
    if not entries:
        return f"{_H_RUMORE}\n{_EMPTY_GROUP}"
    # Cumulative single line, but keep content: each one's short `motivo`, in parens
    # so a motivo that itself contains commas stays unambiguous.
    items = ", ".join(f"{e.nome} ({e.motivo.strip().rstrip('.')})" for e in entries)
    return f"{_H_RUMORE}\n{items}."


# --- TABELLA: one compact plain-text line per conversation ---------------------


def render_table(result: TriageResult) -> str:
    """Compact plain text: one line per conversation (rumore included). No
    monospace/padding — that renders badly on Telegram mobile (dev_notes)."""
    if not result.conversations:
        return _EMPTY_TABLE
    subito, in_corso, rumore = _bucket(result)
    sections = [
        _table_section(_H_SUBITO, subito),
        _table_section(_H_IN_CORSO, in_corso),
        _table_section(_H_RUMORE, rumore),
    ]
    return "\n\n".join(sections)


def _table_section(header: str, entries: list[ConversationTriage]) -> str:
    if not entries:
        return f"{header}\n{_EMPTY_GROUP}"
    lines = [header]
    lines.extend(_table_row(entry) for entry in entries)
    return "\n".join(lines)


def _table_row(entry: ConversationTriage) -> str:
    meta = f"{entry.urgenza.value} · {entry.presidio.value} · {entry.temperatura.value}"
    stato = _one_line(entry.stato_sintetico)
    # SEAM T4: a terse memory tag (e.g. " [promessa scaduta]") would be appended here.
    return f"{entry.nome} — {meta} — {stato}"


# --- VOCALE: synthetic, TTS-oriented -------------------------------------------


def render_voice(result: TriageResult) -> str:
    """Synthetic and TTS-oriented: opens with the urgency, narrates only the SUBITO
    items, then one panoramica sentence for the rest. No bullet lists."""
    if not result.conversations:
        return _EMPTY_VOICE
    subito, in_corso, rumore = _bucket(result)
    opener = _voice_urgent(subito) if subito else "Niente di urgente."
    panoramica = _panoramica(in_corso, rumore, after_urgent=bool(subito))
    return "\n\n".join(p for p in (opener, panoramica) if p)


def _voice_urgent(subito: list[ConversationTriage]) -> str:
    spoken_items = [_voice_item(entry) for entry in subito]
    if len(subito) == 1:
        return "Una cosa da gestire subito: " + spoken_items[0]
    head = f"{_count_word(len(subito)).capitalize()} cose da gestire subito. "
    return head + " ".join(spoken_items)


def _voice_item(entry: ConversationTriage) -> str:
    # Voce = sintetico: si legge `motivo` (frase secca di una riga, per costruzione),
    # NON `stato_sintetico` (paragrafo: resta a schema/tabella). Poi l'azione.
    spoken = _as_sentence(" ".join(entry.motivo.split()))
    action = entry.azione_suggerita.strip()
    if action:
        action = action[:1].upper() + action[1:]
        spoken = f"{spoken} {_as_sentence(action)}"
    return spoken


def _panoramica(
    in_corso: list[ConversationTriage],
    rumore: list[ConversationTriage],
    *,
    after_urgent: bool,
) -> str:
    """One sentence aggregating the non-urgent rest: counts + presidio check."""
    segments: list[str] = []
    if in_corso:
        segments.append(_panoramica_in_corso(in_corso))
    if rumore:
        r = len(rumore)
        segments.append(f"{_count_word(r)} {'voce' if r == 1 else 'voci'} di rumore di fondo")
    if not segments:
        return "Non c'è altro da segnalare." if after_urgent else ""
    body = ", più ".join(segments)
    return f"Per il resto, {body}." if after_urgent else f"Ci sono {body}."


def _panoramica_in_corso(in_corso: list[ConversationTriage]) -> str:
    n = len(in_corso)
    noun = "conversazione in corso" if n == 1 else "conversazioni in corso"
    scoperte = sum(1 for e in in_corso if e.presidio is Presidio.SCOPERTA)
    if scoperte == 0:
        presidio = "presidiata" if n == 1 else "tutte presidiate"
    elif scoperte == n:
        presidio = "ancora scoperta" if n == 1 else "tutte ancora scoperte"
    else:
        presidio = f"{_count_word(scoperte)} ancora {'scoperta' if scoperte == 1 else 'scoperte'}"
    return f"{_count_word(n)} {noun}, {presidio}"


# --- Convenience container (fields = T7 triage_runs columns) -------------------


@dataclass(frozen=True)
class RenderedTriage:
    """The three rendered outputs. Field names mirror T7's ``triage_runs`` columns
    so persistence (T7) and delivery (T8) can consume one typed object."""

    schema_text: str
    table_text: str
    vocal_text: str


def render_all(result: TriageResult) -> RenderedTriage:
    """Render all three formats from a single :class:`TriageResult`."""
    return RenderedTriage(
        schema_text=render_schema(result),
        table_text=render_table(result),
        vocal_text=render_voice(result),
    )
