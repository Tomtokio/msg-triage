"""Unit tests for the three renderers. No network, no mock library.

Renderers are pure functions over a TriageResult, so tests construct
ConversationTriage/TriageResult directly and assert on the produced strings — the
simplest style in the suite (mirrors tests/test_triage_engine.py).
"""

from msg_triage.renderers import (
    _PRESIDIO_SYMBOL,
    _TEMPERATURA_SYMBOL,
    _URGENZA_DOT,
    RenderedTriage,
    _bucket,
    _memory_clause,
    _one_line,
    _schema_symbols,
    _table_symbols,
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

    row = next(l for l in render_table(r).splitlines() if "Sig.ra Rossi" in l)
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


def test_schema_rumore_bolds_name_keeps_motivo_in_one_cumulative_line():
    r = _result(
        _entry(gruppo=Gruppo.RUMORE, nome="Blu", motivo="chiedeva gli orari"),
        _entry(gruppo=Gruppo.RUMORE, nome="Verde", contact_id="c2", motivo="animale trovato, alla Lipu"),
    )
    assert (
        "RUMORE DI FONDO\n<b>Blu</b> (chiedeva gli orari), <b>Verde</b> (animale trovato, alla Lipu)."
        in render_schema(r)
    )


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


def test_table_row_has_symbols_bold_name_and_no_enum_triplet():
    r = _result(
        _entry(gruppo=Gruppo.SUBITO, nome="S", urgenza=Urgenza.ALTA, presidio=Presidio.SCOPERTA, temperatura=Temperatura.MEDIA),
        _entry(gruppo=Gruppo.IN_CORSO, nome="I"),  # media / presidiata / bassa (defaults)
        _entry(gruppo=Gruppo.RUMORE, nome="R"),
    )
    table = render_table(r)
    sym_s = (
        _URGENZA_DOT[Urgenza.ALTA]
        + _PRESIDIO_SYMBOL[Presidio.SCOPERTA]
        + _TEMPERATURA_SYMBOL[Temperatura.MEDIA]
    )
    sym_default = (
        _URGENZA_DOT[Urgenza.MEDIA]
        + _PRESIDIO_SYMBOL[Presidio.PRESIDIATA]
        + _TEMPERATURA_SYMBOL[Temperatura.BASSA]
    )
    assert f"{sym_s} <b>S</b> — " in table
    assert f"{sym_default} <b>I</b> — " in table
    assert f"{sym_default} <b>R</b> — " in table
    assert "·" not in table  # the "urgenza · presidio · temperatura" triplet is gone
    assert len([l for l in table.splitlines() if " — " in l]) == 3


def test_table_strips_leading_client_name_from_prose():
    r = _result(
        _entry(
            gruppo=Gruppo.IN_CORSO,
            nome="Sig.ra Rossi",
            stato_sintetico="Sig.ra Rossi chiede quando è pronta la ricetta.",
        )
    )
    row = next(l for l in render_table(r).splitlines() if "<b>Sig.ra Rossi</b>" in l)
    assert row.count("Sig.ra Rossi") == 1  # only in the bold prefix, not doubled in prose
    assert "<b>Sig.ra Rossi</b> — Chiede quando" in row
    # Schema is untouched: the name stays wherever the model wove it into the prose.
    assert "Sig.ra Rossi chiede" in render_schema(r)


def test_table_keeps_prose_when_name_is_not_leading():
    r = _result(
        _entry(gruppo=Gruppo.IN_CORSO, nome="Bianchi", stato_sintetico="La signora Bianchi aspetta conferma.")
    )
    row = next(l for l in render_table(r).splitlines() if "<b>Bianchi</b>" in l)
    assert "La signora Bianchi aspetta" in row  # not stripped: the name is mid-sentence


def test_table_strips_stray_marker_left_by_truncation():
    # A multi-word species marker cut mid-pair by the 80-char truncation.
    stato = ("parola " * 9) + "**parrocchetto australiano** che sta male"
    r = _result(_entry(gruppo=Gruppo.IN_CORSO, nome="Z", stato_sintetico=stato))
    row = next(l for l in render_table(r).splitlines() if "<b>Z</b>" in l)
    assert "**" not in row  # the stray opening marker is removed
    assert row.endswith("…")  # the row was truncated


# --- HTML markup: escaping, italic species, status symbols ---------------------


def test_html_escaping_of_dynamic_text_in_schema_and_table():
    r = _result(
        _entry(
            gruppo=Gruppo.SUBITO,
            nome="Rossi & <Co>",
            urgenza=Urgenza.ALTA,
            presidio=Presidio.SCOPERTA,
            stato_sintetico="dubbio su <dosaggio> & tempi",
        )
    )
    schema = render_schema(r)
    table = render_table(r)
    # Client/model text is escaped; raw angle brackets/ampersands never reach output.
    assert "&lt;dosaggio&gt;" in schema and "&amp;" in schema
    assert "<dosaggio>" not in schema
    # The name is a real field in the table row: escaped inside our <b> tag.
    assert "<b>Rossi &amp; &lt;Co&gt;</b>" in table
    assert "&lt;dosaggio&gt;" in table


def test_species_marker_becomes_italic_in_schema_and_table():
    r = _result(
        _entry(gruppo=Gruppo.IN_CORSO, nome="Neri", stato_sintetico="la **tartaruga** Ruga non mangia")
    )
    schema = render_schema(r)
    table = render_table(r)
    assert "<i>tartaruga</i>" in schema and "<i>tartaruga</i>" in table
    assert "**" not in schema and "**" not in table


def test_no_species_marker_leaves_no_italic():
    r = _result(_entry(gruppo=Gruppo.IN_CORSO, stato_sintetico="nessuna specie da marcare qui"))
    assert "<i>" not in render_schema(r)
    assert "<i>" not in render_table(r)


def test_table_symbols_map_every_enum_value():
    # Urgency dot: always present, one per value.
    for urgenza, dot in _URGENZA_DOT.items():
        e = _entry(gruppo=Gruppo.IN_CORSO, urgenza=urgenza, presidio=Presidio.PRESIDIATA, temperatura=Temperatura.BASSA)
        assert _table_symbols(e) == dot + _PRESIDIO_SYMBOL[Presidio.PRESIDIATA] + _TEMPERATURA_SYMBOL[Temperatura.BASSA]
    # Presidio: both values map to their symbol.
    for presidio in Presidio:
        e = _entry(gruppo=Gruppo.IN_CORSO, presidio=presidio, temperatura=Temperatura.BASSA)
        assert _PRESIDIO_SYMBOL[presidio] in _table_symbols(e)
    # Temperature: hot/warm add a trailing symbol, calm adds nothing.
    assert _TEMPERATURA_SYMBOL[Temperatura.BASSA] == ""
    for temp in (Temperatura.ALTA, Temperatura.MEDIA):
        e = _entry(gruppo=Gruppo.IN_CORSO, presidio=Presidio.PRESIDIATA, temperatura=temp)
        assert _table_symbols(e).endswith(_TEMPERATURA_SYMBOL[temp])


def test_temperatura_bassa_adds_no_temperature_symbol():
    e = _entry(gruppo=Gruppo.IN_CORSO, urgenza=Urgenza.MEDIA, presidio=Presidio.SCOPERTA, temperatura=Temperatura.BASSA)
    assert _table_symbols(e) == _URGENZA_DOT[Urgenza.MEDIA] + _PRESIDIO_SYMBOL[Presidio.SCOPERTA]


def test_schema_symbols_are_lighter_than_table():
    # presidiata + warm(media): the schema shows only the urgency dot (no ✅, no ⚠️).
    calm = _entry(gruppo=Gruppo.IN_CORSO, urgenza=Urgenza.MEDIA, presidio=Presidio.PRESIDIATA, temperatura=Temperatura.MEDIA)
    assert _schema_symbols(calm) == _URGENZA_DOT[Urgenza.MEDIA]
    # scoperta + hot: dot + attention marks ❗ and 🔥.
    hot = _entry(gruppo=Gruppo.SUBITO, urgenza=Urgenza.ALTA, presidio=Presidio.SCOPERTA, temperatura=Temperatura.ALTA)
    assert _schema_symbols(hot) == (
        _URGENZA_DOT[Urgenza.ALTA] + _PRESIDIO_SYMBOL[Presidio.SCOPERTA] + _TEMPERATURA_SYMBOL[Temperatura.ALTA]
    )


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


def test_voice_stays_plain_strips_marker_no_tags_no_symbols():
    r = _result(
        _entry(
            gruppo=Gruppo.SUBITO,
            nome="Verdi",
            urgenza=Urgenza.ALTA,
            presidio=Presidio.SCOPERTA,
            temperatura=Temperatura.ALTA,
            motivo="la **tartaruga** è bloccata in farmacia",
            azione_suggerita="chiamare la farmacia",
        ),
        _entry(gruppo=Gruppo.IN_CORSO, presidio=Presidio.SCOPERTA),
    )
    voice = render_voice(r)
    assert "**" not in voice  # species marker stripped for the spoken text
    assert "<i>" not in voice and "<b>" not in voice
    symbols = set(_URGENZA_DOT.values()) | set(_PRESIDIO_SYMBOL.values()) | set(_TEMPERATURA_SYMBOL.values())
    for symbol in symbols:
        if symbol:  # skip the empty temperatura-bassa marker
            assert symbol not in voice
    assert "tartaruga" in voice  # the species word survives, just unmarked


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
