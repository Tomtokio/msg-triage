"""Unit tests for the three renderers. No network, no mock library.

Renderers are pure functions over a TriageResult, so tests construct
ConversationTriage/TriageResult directly and assert on the produced strings — the
simplest style in the suite (mirrors tests/test_triage_engine.py).
"""

from msg_triage.renderers import (
    RenderedTriage,
    _bucket,
    _memory_clause,
    _one_line,
    render_all,
    render_schema,
    render_table,
    render_voice,
)
from msg_triage.triage_engine import (
    ConversationTriage,
    Gruppo,
    Presidio,
    Promessa,
    Temperatura,
    TriageResult,
    Urgenza,
)


def _entry(
    *,
    contact_id: str = "c1",
    nome: str = "Sig.ra Rossi",
    gruppo: Gruppo = Gruppo.IN_CORSO,
    motivo: str = "m",
    urgenza: Urgenza = Urgenza.MEDIA,
    presidio: Presidio = Presidio.PRESIDIATA,
    temperatura: Temperatura = Temperatura.BASSA,
    stato_sintetico: str = "stato",
    azione_suggerita: str = "",
    promessa_rilevata: Promessa | None = None,
) -> ConversationTriage:
    return ConversationTriage(
        contact_id=contact_id,
        nome=nome,
        gruppo=gruppo,
        motivo=motivo,
        urgenza=urgenza,
        presidio=presidio,
        temperatura=temperatura,
        stato_sintetico=stato_sintetico,
        azione_suggerita=azione_suggerita,
        promessa_rilevata=promessa_rilevata,
    )


def _result(*entries: ConversationTriage) -> TriageResult:
    return TriageResult(conversations=tuple(entries))


# --- Empty states --------------------------------------------------------------


def test_empty_result_returns_italian_all_clear():
    r = _result()
    assert "Nessuna conversazione con attività recente" in render_schema(r)
    assert "Nessuna conversazione con attività recente" in render_table(r)
    assert "Tutto tranquillo" in render_voice(r)


def test_empty_group_shows_placeholder_line():
    r = _result(_entry(gruppo=Gruppo.IN_CORSO))
    assert "DA GESTIRE SUBITO\nNessuna, per ora." in render_schema(r)
    table = render_table(r)
    assert "DA GESTIRE SUBITO\nNessuna, per ora." in table
    assert "RUMORE DI FONDO\nNessuna, per ora." in table


# --- Grouping and ordering -----------------------------------------------------


def test_bucket_groups_and_sorts_by_urgency():
    a = _entry(contact_id="a", gruppo=Gruppo.SUBITO, urgenza=Urgenza.ALTA)
    b = _entry(contact_id="b", gruppo=Gruppo.IN_CORSO)
    c = _entry(contact_id="c", gruppo=Gruppo.SUBITO, urgenza=Urgenza.EMERGENZA)
    d = _entry(contact_id="d", gruppo=Gruppo.RUMORE)
    subito, in_corso, rumore = _bucket(_result(a, b, c, d))
    assert [e.contact_id for e in subito] == ["c", "a"]  # emergenza before alta
    assert [e.contact_id for e in in_corso] == ["b"]
    assert [e.contact_id for e in rumore] == ["d"]


def test_bucket_stable_tiebreak_preserves_model_order():
    x = _entry(contact_id="x", gruppo=Gruppo.IN_CORSO)
    y = _entry(contact_id="y", gruppo=Gruppo.IN_CORSO)
    _, in_corso, _ = _bucket(_result(x, y))
    assert [e.contact_id for e in in_corso] == ["x", "y"]


def test_schema_sections_in_fixed_order():
    r = _result(
        _entry(gruppo=Gruppo.RUMORE, nome="R"),
        _entry(gruppo=Gruppo.SUBITO, nome="S", urgenza=Urgenza.ALTA, presidio=Presidio.SCOPERTA),
        _entry(gruppo=Gruppo.IN_CORSO, nome="I"),
    )
    schema = render_schema(r)
    assert schema.index("DA GESTIRE SUBITO") < schema.index("IN CORSO") < schema.index("RUMORE DI FONDO")


# --- SCHEMA --------------------------------------------------------------------


def test_schema_keeps_stato_verbatim_table_collapses_and_truncates():
    long_stato = (
        "Riga uno molto lunga.\nRiga due.\n"
        "Riga tre con parecchie parole in più per superare il limite di ottanta caratteri."
    )
    r = _result(_entry(gruppo=Gruppo.IN_CORSO, stato_sintetico=long_stato))
    assert long_stato.strip() in render_schema(r)  # verbatim, newlines preserved

    row = next(l for l in render_table(r).splitlines() if l.startswith("Sig.ra Rossi"))
    assert "\n" not in row
    assert row.endswith("…")


def test_schema_shows_azione_when_present_and_hides_when_empty():
    with_action = render_schema(
        _result(
            _entry(
                gruppo=Gruppo.SUBITO,
                urgenza=Urgenza.ALTA,
                presidio=Presidio.SCOPERTA,
                azione_suggerita="chiamare la farmacia",
            )
        )
    )
    assert "Da fare: chiamare la farmacia." in with_action
    assert "Da fare:" not in render_schema(_result(_entry(azione_suggerita="")))


def test_schema_azione_no_double_period():
    schema = render_schema(
        _result(
            _entry(
                gruppo=Gruppo.SUBITO,
                urgenza=Urgenza.ALTA,
                presidio=Presidio.SCOPERTA,
                azione_suggerita="rifare la ricetta.",  # already ends with a period
            )
        )
    )
    assert "Da fare: rifare la ricetta." in schema
    assert ".." not in schema


def test_schema_rumore_keeps_motivo_in_one_cumulative_line():
    r = _result(
        _entry(gruppo=Gruppo.RUMORE, nome="Blu", motivo="chiedeva gli orari"),
        _entry(gruppo=Gruppo.RUMORE, nome="Verde", contact_id="c2", motivo="animale trovato, alla Lipu"),
    )
    assert "RUMORE DI FONDO\nBlu (chiedeva gli orari), Verde (animale trovato, alla Lipu)." in render_schema(r)


# --- Memory seam (T4): silent today --------------------------------------------


def test_memory_clause_is_empty_today():
    assert _memory_clause(_entry()) == ""


def test_no_memory_phrases_rendered_today():
    r = _result(
        _entry(gruppo=Gruppo.SUBITO, urgenza=Urgenza.ALTA, presidio=Presidio.SCOPERTA, azione_suggerita="x"),
        _entry(
            gruppo=Gruppo.IN_CORSO,
            promessa_rilevata=Promessa("le confermo entro due ore", "2026-07-17 14:00"),
        ),
    )
    for text in (render_schema(r), render_table(r), render_voice(r)):
        low = text.lower()
        assert "non mantenut" not in low
        assert "scaduta" not in low
        assert "run precedente" not in low
        assert "promessa" not in low  # promessa_rilevata is not re-rendered in v0
        assert "[" not in text  # no terse memory tags yet


# --- TABELLA -------------------------------------------------------------------


def test_table_has_one_row_per_conversation_with_enums():
    r = _result(
        _entry(gruppo=Gruppo.SUBITO, nome="S", urgenza=Urgenza.ALTA, presidio=Presidio.SCOPERTA, temperatura=Temperatura.MEDIA),
        _entry(gruppo=Gruppo.IN_CORSO, nome="I"),
        _entry(gruppo=Gruppo.RUMORE, nome="R"),
    )
    table = render_table(r)
    assert "S — alta · scoperta · media — " in table
    assert "I — media · presidiata · bassa — " in table
    assert "R — media · presidiata · bassa — " in table
    assert len([l for l in table.splitlines() if " — " in l]) == 3


# --- VOCALE --------------------------------------------------------------------


def test_voice_opens_with_urgency_and_summarizes_rest():
    r = _result(
        _entry(
            gruppo=Gruppo.SUBITO,
            nome="Sig. Verdi",
            urgenza=Urgenza.ALTA,
            presidio=Presidio.SCOPERTA,
            motivo="bloccato in farmacia, ricetta sbagliata",
            stato_sintetico="DETTAGLIO_STATO_NON_VOCALE",
            azione_suggerita="chiamare la farmacia",
        ),
        _entry(gruppo=Gruppo.IN_CORSO, nome="Bianchi", motivo="MOTIVO_INCORSO", stato_sintetico="DETTAGLIO_INCORSO"),
        _entry(gruppo=Gruppo.IN_CORSO, nome="Amir", contact_id="c3"),
    )
    voice = render_voice(r)
    assert voice.startswith("Una cosa da gestire subito:")
    assert "bloccato in farmacia, ricetta sbagliata" in voice  # spoken from `motivo`
    assert "Chiamare la farmacia." in voice  # azione, capitalized, single period
    assert "DETTAGLIO_STATO_NON_VOCALE" not in voice  # stato_sintetico is NOT spoken
    assert "MOTIVO_INCORSO" not in voice  # in-corso items are NOT narrated aloud
    assert "due conversazioni in corso" in voice
    assert "presidiate" in voice
    assert "•" not in voice and "\n- " not in voice  # no bullet lists


def test_voice_no_urgency_reassures():
    r = _result(
        _entry(gruppo=Gruppo.IN_CORSO),
        _entry(gruppo=Gruppo.IN_CORSO, contact_id="c2"),
    )
    voice = render_voice(r)
    assert voice.startswith("Niente di urgente.")
    assert "due conversazioni in corso" in voice
    assert "tutte presidiate" in voice


def test_voice_flags_uncovered_in_panoramica():
    r = _result(
        _entry(gruppo=Gruppo.IN_CORSO, presidio=Presidio.SCOPERTA),
        _entry(gruppo=Gruppo.IN_CORSO, contact_id="c2", presidio=Presidio.PRESIDIATA),
    )
    assert "una ancora scoperta" in render_voice(r)


# --- Helpers, container, determinism -------------------------------------------


def test_one_line_collapses_and_truncates():
    assert _one_line("a  b\nc") == "a b c"
    out = _one_line("parola " * 30, limit=20)
    assert out.endswith("…")
    assert "\n" not in out
    assert len(out) <= 21


def test_render_all_matches_individual_renderers():
    r = _result(_entry(gruppo=Gruppo.IN_CORSO))
    rendered = render_all(r)
    assert isinstance(rendered, RenderedTriage)
    assert rendered.schema_text == render_schema(r)
    assert rendered.table_text == render_table(r)
    assert rendered.vocal_text == render_voice(r)


def test_renderers_are_deterministic():
    r = _result(
        _entry(contact_id="a", gruppo=Gruppo.SUBITO, urgenza=Urgenza.ALTA, presidio=Presidio.SCOPERTA),
        _entry(contact_id="b", gruppo=Gruppo.IN_CORSO),
        _entry(contact_id="c", gruppo=Gruppo.RUMORE),
    )
    for render in (render_schema, render_table, render_voice):
        assert render(r) == render(r)
