"""Renderers: one structured :class:`TriageResult` -> three text outputs.

Design principle (dev_notes n.2): the triage produces ONE structured object with a
single LLM call; here we derive THREE depths of rendering from it, deterministically
and with no further inference. The narrative detail is already baked into
``stato_sintetico`` by the model (the prompt doses length by temperature), so these
renderers only lay it out — they never re-summarize prose.

- ``render_schema`` = giornale di bordo completo, prosa a tre livelli.
- ``render_table``  = cruscotto operativo: una riga per conversazione con l'azione da
                      fare (``azione_suggerita``, ripiego su ``motivo``); testo semplice,
                      niente tabelle monospace fragili su Telegram mobile.
- ``render_voice``  = schema raccontato per l'ascolto: apre dall'urgenza, narra OGNI
                      conversazione una frase — col nome, dal campo breve ``motivo``
                      (+ azione), mai dal paragrafo ``stato_sintetico`` — e comprime il
                      rumore in una frase cumulativa. Prosa continua, niente simboli né
                      elenchi.

Both HTML formats treat RUMORE as a group rather than as conversations: it requires no
action, so it carries one neutral ⚪ and none of the presidio/temperatura marks (the
model fills those unreliably there), and the schema merges the names sharing a ``motivo``.

Schema and table are Telegram HTML (``parse_mode="HTML"``): model-authored text is
``html.escape``-d FIRST, then OUR ``<b>``/``<i>`` tags and status symbols are added
around it. The voice stays plain text (no tags, no symbols) for TTS. The three
strings map 1:1 to T7's ``triage_runs`` columns via :class:`RenderedTriage`.

Memory (T4) seam: memory signals do not exist on the triage object yet. The single
insertion point is :func:`_memory_clause` (returns ``""`` today); ``render_schema``
already splices it, and the table/voice insertion points are marked. When T4 lands
it will compose the Italian delta phrase there. See ``docs/tasks.md`` T4.
"""

from __future__ import annotations

import html
import re
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

_TABLE_ROW_LIMIT = 120  # chars of azione/motivo kept in a row (safety net; one-liners by design)


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
    Python's stable sort preserves model order for equal keys. ``rumore`` keeps model
    order because that order is visible: the schema groups it by ``motivo``, and both
    the groups and the names inside one follow first appearance.
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


def _agree(n: int, singular: str, plural: str) -> str:
    """Pick the Italian wording that agrees with ``n``.

    The voice text is fed to TTS, where a number mismatch ("Ci sono una
    conversazione") is far more audible than it is readable, so every part of the
    panoramica that inflects goes through here instead of an inline conditional.
    """
    return singular if n == 1 else plural


def _one_line(text: str, limit: int = _TABLE_ROW_LIMIT) -> str:
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


# --- Visual markup: HTML + status symbols (schema/table only, never the voice) --
#
# Schema and table go out as Telegram HTML. Model-authored text is escaped FIRST,
# then we wrap OUR tags around it — never the reverse. The species italic comes from
# a ``**specie**`` marker the model emits (docs/triage_system_prompt.md, "Come
# scrivere"); the regex matches only balanced ``**`` pairs, so a lone marker stays
# literal and can never open an ``<i>`` that isn't closed. The voice strips the
# marker instead (plain text: no tags, no symbols).

_URGENZA_DOT = {
    Urgenza.EMERGENZA: "🔴",
    Urgenza.ALTA: "🟠",
    Urgenza.MEDIA: "🟡",
    Urgenza.BASSA: "⚪",
}
_PRESIDIO_SYMBOL = {Presidio.SCOPERTA: "❗", Presidio.PRESIDIATA: "✅"}
_TEMPERATURA_SYMBOL = {Temperatura.ALTA: "🔥", Temperatura.MEDIA: "⚠️", Temperatura.BASSA: ""}

# RUMORE is a group, not a severity: it needs no action, so the schema line and the
# table row both carry this single neutral dot instead of the urgenza/presidio/
# temperatura marks. Same glyph as the "bassa" urgency dot by coincidence — its own
# constant because the two meanings are unrelated and must be free to drift apart.
_RUMORE_DOT = "⚪"

_SPECIES_MARKER = re.compile(r"\*\*(.+?)\*\*")  # only balanced pairs -> <i>...</i>


def _table_symbols(entry: ConversationTriage) -> str:
    """Full symbol prefix for a table row: urgency dot + presidio + temperature.

    RUMORE short-circuits to the neutral dot alone: the group requires no action, so
    ❗/✅ and 🔥/⚠️ would be noise there — and the model fills those fields unreliably
    for it (seen live: ❗ on a chat with zero client messages). Deterministic before
    inference: we do not show a judgment where it means nothing, so the bug dies by
    construction. The temperature symbol is ``""`` for ``bassa`` (calm is the norm)."""
    if entry.gruppo is Gruppo.RUMORE:
        return _RUMORE_DOT
    return (
        _URGENZA_DOT[entry.urgenza]
        + _PRESIDIO_SYMBOL[entry.presidio]
        + _TEMPERATURA_SYMBOL[entry.temperatura]
    )


def _schema_symbols(entry: ConversationTriage) -> str:
    """Lighter prefix for a schema paragraph: urgency dot + only the *attention*
    marks (❗ if uncovered, 🔥 if hot). No ✅/⚠️ — the schema stays a text to read."""
    symbols = _URGENZA_DOT[entry.urgenza]
    if entry.presidio is Presidio.SCOPERTA:
        symbols += _PRESIDIO_SYMBOL[Presidio.SCOPERTA]
    if entry.temperatura is Temperatura.ALTA:
        symbols += _TEMPERATURA_SYMBOL[Temperatura.ALTA]
    return symbols


def _html(raw: str) -> str:
    """Escape model text for Telegram HTML, then turn the ``**specie**`` marker into
    ``<i>specie</i>``. Escape first so any ``< > &`` the client typed become entities;
    our italic tags are added around the already-escaped text."""
    return _SPECIES_MARKER.sub(r"<i>\1</i>", html.escape(raw, quote=False))


def _strip_species_marker(raw: str) -> str:
    """Drop the ``**`` species marker for the voice (plain text: no tags, no marks)."""
    return _SPECIES_MARKER.sub(r"\1", raw)


# --- SCHEMA: three-level prose, complete giornale di bordo ---------------------


def render_schema(result: TriageResult) -> str:
    """Three-level prose. One paragraph per conversation in SUBITO/IN CORSO; RUMORE
    collapses to one line per distinct ``motivo``, with the names sharing it merged.
    Opens with the most urgent group.
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
    # Nome NON anteposto: resta nella prosa del modello (stato_sintetico). Il
    # paragrafo apre coi simboli leggeri, poi la prosa (HTML: escape + corsivo specie).
    parts = [_html(entry.stato_sintetico.strip())]
    clause = _memory_clause(entry)  # SEAM T4: "" today
    if clause:
        parts.append(clause)
    action = _as_sentence(entry.azione_suggerita)  # azione may already end with "."
    if action:
        parts.append(f"Da fare: {_html(action)}")
    return f"{_schema_symbols(entry)} " + " ".join(parts)


def _schema_rumore_section(entries: list[ConversationTriage]) -> str:
    if not entries:
        return f"{_H_RUMORE}\n{_EMPTY_GROUP}"
    # One line per distinct motivo, names merged: the output schema forces one entry per
    # conversation, so only the renderer can collapse the repetition (seen live: three
    # separate "(Conversazione chiusa)"). The key drops the final period the model adds
    # erratically and IS the text we print, so one motivo = one wording. Dict order =
    # first appearance, for the groups and for the names inside one. Names in bold (a
    # real field here), motivo through _html (escape + species italic), and _as_sentence
    # for exactly one closing mark — it also lands the period outside a trailing </i>.
    grouped: dict[str, list[str]] = {}  # motivo -> already-escaped bold names
    for entry in entries:
        key = entry.motivo.strip().rstrip(".")
        grouped.setdefault(key, []).append(f"<b>{html.escape(entry.nome, quote=False)}</b>")
    lines = [_H_RUMORE]
    for motivo, names in grouped.items():
        tail = f": {_html(motivo)}" if motivo else ""  # nothing left to say -> names alone
        lines.append(_as_sentence(f"{_RUMORE_DOT} {', '.join(names)}{tail}"))
    return "\n".join(lines)


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
    # Dashboard, not narration: the row shows the ACTION to take. ``azione_suggerita`` is
    # already a short operative line; fall back to ``motivo`` (the one-line fact) when the
    # model left no action — typical of rumore and concluded chats — and to symbols + name
    # alone when both are empty. Rendered verbatim (no re-capitalization): azione/motivo
    # don't lead with the client name (the bold prefix already carries it), so the old
    # stato-prose name-dedup is gone. Truncate on the RAW text (a safety net — these fields
    # are one-liners by design), THEN escape + italic; truncation can cut inside a
    # ``**...**`` pair, so strip any residual marker (seen live: "il suo **parrocchetto…").
    symbols = _table_symbols(entry)
    name = f"<b>{html.escape(entry.nome, quote=False)}</b>"
    text = entry.azione_suggerita.strip() or entry.motivo.strip()
    # SEAM T4: a terse memory tag (e.g. " [promessa scaduta]") would be appended here.
    if not text:
        return f"{symbols} {name}"
    body = _html(_one_line(text)).replace("**", "")
    return f"{symbols} {name} — {body}"


# --- VOCALE: schema raccontato, TTS-oriented -----------------------------------

_VOICE_IN_CORSO_CAP = 6  # narrate this many IN CORSO in full; compress the rest to a count


def render_voice(result: TriageResult) -> str:
    """Schema raccontato per l'ascolto (TTS): opens with the urgency (SUBITO) or
    "Niente di urgente.", narrates every IN CORSO one sentence each — by name, from the
    one-line ``motivo`` (+ azione), capped, with a presidio closing — and folds RUMORE
    into one cumulative sentence. Continuous prose: no symbols, no tags, no bullet lists.
    """
    if not result.conversations:
        return _EMPTY_VOICE
    subito, in_corso, rumore = _bucket(result)
    opener = _voice_urgent(subito) if subito else "Niente di urgente."
    parts = [opener, _voice_in_corso(in_corso), _voice_rumore(rumore)]
    return " ".join(p for p in parts if p)  # one continuous paragraph for TTS


def _voice_urgent(subito: list[ConversationTriage]) -> str:
    spoken_items = [_voice_item(entry) for entry in subito]
    if len(subito) == 1:
        return "Una cosa da gestire subito: " + spoken_items[0]
    head = f"{_count_word(len(subito)).capitalize()} cose da gestire subito. "
    return head + " ".join(spoken_items)


def _voice_item(entry: ConversationTriage) -> str:
    # Voce = sintetico: si legge `motivo` (frase secca di una riga, per costruzione),
    # NON `stato_sintetico` (paragrafo: resta a schema/tabella). Il nome va anteposto —
    # chi ascolta non ha contesto davanti e `motivo` non porta il nome del cliente — con
    # un lead-in a virgola, robusto a ogni forma di motivo (verbo/nome/participio).
    # WATCH-LIST (prima cosa da riascoltare sui run reali): "{nome}, {motivo}." separa
    # soggetto e verbo con una virgola — scorretto in italiano scritto, accettabile
    # all'ascolto come stile telegrafico. Se suona male: invertire ("Da parte di {nome},
    # …") o togliere la virgola quando il motivo apre con un verbo. Il marcatore
    # `**specie**` va tolto: il vocale è testo pulito (niente tag, niente **).
    motivo = _strip_species_marker(" ".join(entry.motivo.split())).strip()
    name = entry.nome.strip()
    spoken = _as_sentence(f"{name}, {motivo}" if name and motivo else (name or motivo))
    action = _strip_species_marker(entry.azione_suggerita.strip())
    if action:
        action = _as_sentence(action[:1].upper() + action[1:])
        spoken = f"{spoken} {action}".strip()
    return spoken


def _voice_in_corso(in_corso: list[ConversationTriage]) -> str:
    """Narrate the IN CORSO one sentence each (capped at ``_VOICE_IN_CORSO_CAP``), then a
    presidio closing over the narrated ones, then — if capped — a count tail for the rest.
    Entries arrive urgency-sorted from :func:`_bucket`, so the cap keeps the most urgent.
    """
    if not in_corso:
        return ""
    cap = _VOICE_IN_CORSO_CAP
    capped = cap is not None and len(in_corso) > cap
    narrated = in_corso[:cap] if capped else in_corso
    parts = [_voice_item(entry) for entry in narrated]
    waiting = _voice_in_corso_waiting(narrated)
    if waiting:
        parts.append(waiting)
    if capped:
        parts.append(_voice_in_corso_tail(in_corso[cap:]))
    return " ".join(parts)


def _voice_in_corso_waiting(narrated: list[ConversationTriage]) -> str:
    """Presidio closing over the narrated IN CORSO: how many still await a reply. Keeps
    the "scoperta" signal alive even with no cap (the common ≤6 day). With ONE narrated
    (thus uncovered) → the pronoun-free "Nessuno ha ancora risposto." (an "Una…" would
    have no antecedent, and one IN CORSO is the frequent real case); with ≥2 → "Di queste,
    {n} aspettano ancora risposta." where "queste" anchors the count. "" if all presidiate.
    """
    scoperte = sum(1 for e in narrated if e.presidio is Presidio.SCOPERTA)
    if scoperte == 0:
        return ""
    if len(narrated) == 1:
        return "Nessuno ha ancora risposto."
    return f"Di queste, {_count_word(scoperte)} {_agree(scoperte, 'aspetta', 'aspettano')} ancora risposta."


def _voice_in_corso_tail(overflow: list[ConversationTriage]) -> str:
    """Count tail for the IN CORSO beyond the cap, carrying its own presidio clause."""
    if len(overflow) == 1:
        scoperte = 1 if overflow[0].presidio is Presidio.SCOPERTA else 0
        return _as_sentence(f"E un'altra conversazione in corso, {_presidio_clause(1, scoperte)}")
    return _as_sentence(f"E altre {_panoramica_in_corso(overflow)}")


def _voice_rumore(rumore: list[ConversationTriage]) -> str:
    """One cumulative closing sentence for the noise. Content-based: the DISTINCT motivos
    (deduped with the schema's key + first-appearance order), so a routed found-animal
    surfaces while a repeated generic motivo is said once. Falls back to a plain count
    when nothing distinct remains (empty/degenerate motivos).
    """
    if not rumore:
        return ""
    keys: list[str] = []
    for entry in rumore:
        key = entry.motivo.strip().rstrip(".")  # same normalization as _schema_rumore_section
        if key and key not in keys:
            keys.append(key)
    if not keys:
        return _voice_rumore_count(rumore)
    parts = [_strip_species_marker(" ".join(k.split())) for k in keys]
    return _as_sentence("Nel rumore di fondo, " + "; ".join(parts))


def _voice_rumore_count(rumore: list[ConversationTriage]) -> str:
    """Plain count fallback, with number agreement (verb + voce/voci)."""
    r = len(rumore)
    verb = _agree(r, "C'è", "Ci sono")
    return f"{verb} {_count_word(r)} {_agree(r, 'voce', 'voci')} di rumore di fondo."


def _presidio_clause(n: int, scoperte: int) -> str:
    """Presidio agreement shared by the in-corso count phrasings (panoramica + cap tail)."""
    if scoperte == 0:
        return _agree(n, "presidiata", "tutte presidiate")
    if scoperte == n:
        return _agree(n, "ancora scoperta", "tutte ancora scoperte")
    return f"{_count_word(scoperte)} ancora {_agree(scoperte, 'scoperta', 'scoperte')}"


def _panoramica_in_corso(in_corso: list[ConversationTriage]) -> str:
    n = len(in_corso)
    noun = _agree(n, "conversazione in corso", "conversazioni in corso")
    scoperte = sum(1 for e in in_corso if e.presidio is Presidio.SCOPERTA)
    return f"{_count_word(n)} {noun}, {_presidio_clause(n, scoperte)}"


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
