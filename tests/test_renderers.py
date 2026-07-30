"""Unit tests for the three renderers. No network, no mock library.

Renderers are pure functions over a TriageResult, so tests construct
ConversationTriage/TriageResult directly and assert on the produced strings — the
simplest style in the suite (mirrors tests/test_triage_engine.py).
"""

from msg_triage.renderers import (
    _PRESIDIO_SYMBOL,
    _RUMORE_DOT,
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


def test_schema_keeps_stato_verbatim():
    long_stato = (
        "Riga uno molto lunga.\nRiga due.\n"
        "Riga tre con parecchie parole in più per superare il limite di ottanta caratteri."
    )
    r = _result(_entry(gruppo=Gruppo.IN_CORSO, stato_sintetico=long_stato))
    assert long_stato.strip() in render_schema(r)  # verbatim, newlines preserved


def test_table_truncates_long_text_as_safety_net():
    # The table renders azione/motivo (one-liners by design); truncation is only a net
    # against a model that ignores "una riga". A long azione collapses to one line + "…".
    long_azione = "chiamare la farmacia " * 20  # well over _TABLE_ROW_LIMIT
    r = _result(_entry(gruppo=Gruppo.IN_CORSO, azione_suggerita=long_azione))
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


def test_schema_rumore_merges_names_sharing_one_motivo():
    # The output schema forces one entry per conversation, so the model cannot merge
    # three identical "Conversazione chiusa" (seen live): the renderer must.
    r = _result(
        _entry(gruppo=Gruppo.RUMORE, nome="Miri", motivo="Conversazione chiusa"),
        _entry(gruppo=Gruppo.RUMORE, nome="Angelica", contact_id="c2", motivo="Chiarimento concluso"),
        _entry(gruppo=Gruppo.RUMORE, nome="Paolo Tosi", contact_id="c3", motivo="Conversazione chiusa"),
    )
    # Group and name order = first appearance: "Conversazione chiusa" opens even though
    # Angelica's entry came second, and Paolo joins Miri on that line.
    assert (
        "RUMORE DI FONDO\n"
        "⚪ <b>Miri</b>, <b>Paolo Tosi</b>: Conversazione chiusa.\n"
        "⚪ <b>Angelica</b>: Chiarimento concluso."
    ) in render_schema(r)


def test_schema_rumore_dedup_ignores_trailing_period_and_padding():
    # The model varies the final period between entries; the key normalizes it away.
    r = _result(
        _entry(gruppo=Gruppo.RUMORE, nome="A", motivo="conversazione chiusa."),
        _entry(gruppo=Gruppo.RUMORE, nome="B", contact_id="c2", motivo=" conversazione chiusa "),
    )
    schema = render_schema(r)
    assert "⚪ <b>A</b>, <b>B</b>: conversazione chiusa." in schema
    assert len([l for l in schema.splitlines() if l.startswith(_RUMORE_DOT)]) == 1


def test_schema_rumore_case_variance_is_not_merged():
    # Deliberate: the key is not casefolded. The key IS the printed text, so folding it
    # would force picking an arbitrary casing to display; the cost of a miss is one
    # extra line carrying fully correct content.
    r = _result(
        _entry(gruppo=Gruppo.RUMORE, nome="A", motivo="Conversazione chiusa"),
        _entry(gruppo=Gruppo.RUMORE, nome="B", contact_id="c2", motivo="conversazione chiusa"),
    )
    assert len([l for l in render_schema(r).splitlines() if l.startswith(_RUMORE_DOT)]) == 2


def test_schema_rumore_line_ends_with_exactly_one_sentence_mark():
    # The key strips the model's erratic period and _as_sentence re-adds exactly one;
    # ?/! survive, and a motivo that normalizes to nothing leaves the names alone.
    r = _result(
        _entry(gruppo=Gruppo.RUMORE, nome="A", motivo="chiedeva gli orari."),
        _entry(gruppo=Gruppo.RUMORE, nome="B", contact_id="c2", motivo="riaprite sabato?"),
        _entry(gruppo=Gruppo.RUMORE, nome="C", contact_id="c3", motivo="."),
    )
    schema = render_schema(r)
    assert "⚪ <b>A</b>: chiedeva gli orari." in schema
    assert "⚪ <b>B</b>: riaprite sabato?" in schema
    assert "⚪ <b>C</b>." in schema  # no dangling ": ."
    assert ".." not in schema and "?." not in schema


def test_schema_rumore_escapes_names_and_italicizes_species():
    r = _result(
        _entry(gruppo=Gruppo.RUMORE, nome="Blu & <Co>", motivo="**passerotto** trovato, alla Lipu")
    )
    schema = render_schema(r)
    assert "<b>Blu &amp; &lt;Co&gt;</b>" in schema
    assert "<i>passerotto</i> trovato, alla Lipu." in schema  # period lands outside </i>
    assert "**" not in schema


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
    assert f"{_RUMORE_DOT} <b>R</b> — " in table  # rumore: neutral dot only
    assert "·" not in table  # the "urgenza · presidio · temperatura" triplet is gone
    assert len([l for l in table.splitlines() if " — " in l]) == 3


def test_table_rumore_row_carries_only_the_neutral_dot():
    # Rumore needs no action and the model fills urgenza/presidio/temperatura unreliably
    # there (live: ❗ on a chat with zero client messages) — the row must not show them.
    noisy = _entry(
        gruppo=Gruppo.RUMORE,
        nome="R",
        urgenza=Urgenza.ALTA,
        presidio=Presidio.SCOPERTA,
        temperatura=Temperatura.ALTA,
    )
    assert _table_symbols(noisy) == _RUMORE_DOT
    row = next(l for l in render_table(_result(noisy)).splitlines() if "<b>R</b>" in l)
    assert row.startswith(f"{_RUMORE_DOT} <b>R</b> — ")
    for symbol in (
        _URGENZA_DOT[Urgenza.ALTA],
        _PRESIDIO_SYMBOL[Presidio.SCOPERTA],
        _TEMPERATURA_SYMBOL[Temperatura.ALTA],
    ):
        assert symbol not in row


def test_table_row_prefers_azione_then_motivo_then_bare():
    # The row shows the action: azione_suggerita wins; motivo is the fallback; when both
    # are empty the row is symbols + bold name alone (no " — ", no trailing text).
    azione = _entry(
        gruppo=Gruppo.IN_CORSO, nome="A", motivo="il coniglio non mangia", azione_suggerita="confermare per stasera"
    )
    row_a = next(l for l in render_table(_result(azione)).splitlines() if "<b>A</b>" in l)
    assert row_a.endswith("— confermare per stasera")
    assert "coniglio" not in row_a  # motivo is not used when azione is present

    only_motivo = _entry(gruppo=Gruppo.IN_CORSO, nome="B", motivo="passante con un pullo di passero", azione_suggerita="   ")
    row_b = next(l for l in render_table(_result(only_motivo)).splitlines() if "<b>B</b>" in l)
    assert row_b.endswith("— passante con un pullo di passero")  # whitespace azione falls back to motivo

    bare = _entry(gruppo=Gruppo.IN_CORSO, nome="C", motivo="", azione_suggerita="")
    row_c = next(l for l in render_table(_result(bare)).splitlines() if "<b>C</b>" in l)
    assert row_c.endswith("<b>C</b>")  # bare: no " — ", no text
    assert "—" not in row_c


def test_table_strips_stray_marker_left_by_truncation():
    # A multi-word species marker cut mid-pair by the _TABLE_ROW_LIMIT truncation: the
    # balanced-only regex leaves the stray opening ``**``, which _table_row must strip.
    motivo = ("parola " * 15) + "**parrocchetto australiano** che sta male"
    r = _result(_entry(gruppo=Gruppo.IN_CORSO, nome="Z", motivo=motivo))
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
            azione_suggerita="verificare <dosaggio> & tempi",
        )
    )
    schema = render_schema(r)
    table = render_table(r)
    # Client/model text is escaped; raw angle brackets/ampersands never reach output.
    assert "&lt;dosaggio&gt;" in schema and "&amp;" in schema
    assert "<dosaggio>" not in schema
    # The name is a real field in the table row: escaped inside our <b> tag.
    assert "<b>Rossi &amp; &lt;Co&gt;</b>" in table
    # The table now renders azione (escaped): raw brackets never reach the output.
    assert "&lt;dosaggio&gt;" in table
    assert "<dosaggio>" not in table


def test_species_marker_becomes_italic_in_schema_and_table():
    r = _result(
        _entry(
            gruppo=Gruppo.IN_CORSO,
            nome="Neri",
            motivo="la **tartaruga** Ruga non mangia",
            stato_sintetico="la **tartaruga** Ruga non mangia",
        )
    )
    schema = render_schema(r)
    table = render_table(r)  # azione empty -> falls back to motivo, which carries the marker
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


# --- VOCALE: accordo di numero nella panoramica --------------------------------
# Il vocale finisce in sintesi vocale (T6): una concordanza sbagliata si SENTE.
# Seen live: "Ci sono una conversazione in corso, presidiata, più una voce di
# rumore di fondo" — il verbo era plurale fisso.


def test_voice_panoramica_singular_agreement():
    r = _result(
        _entry(gruppo=Gruppo.IN_CORSO),
        _entry(gruppo=Gruppo.RUMORE, contact_id="c2"),
    )
    voice = render_voice(r)
    assert "C'è una conversazione in corso, presidiata, più una voce di rumore di fondo." in voice
    assert "Ci sono una" not in voice
    assert "più una voci" not in voice


def test_voice_panoramica_singular_uncovered():
    r = _result(_entry(gruppo=Gruppo.IN_CORSO, presidio=Presidio.SCOPERTA))
    assert "C'è una conversazione in corso, ancora scoperta." in render_voice(r)


def test_voice_panoramica_rumore_only_agrees_with_its_own_count():
    one = _result(_entry(gruppo=Gruppo.RUMORE))
    assert "C'è una voce di rumore di fondo." in render_voice(one)
    three = _result(*(_entry(gruppo=Gruppo.RUMORE, contact_id=f"c{i}") for i in range(3)))
    assert "Ci sono tre voci di rumore di fondo." in render_voice(three)


def test_voice_panoramica_plural_verb_with_singular_tail():
    # Con soggetti coordinati da "più" il verbo accorda col PRIMO, non col totale.
    r = _result(
        _entry(gruppo=Gruppo.IN_CORSO),
        _entry(gruppo=Gruppo.IN_CORSO, contact_id="c2"),
        _entry(gruppo=Gruppo.RUMORE, contact_id="c3"),
    )
    voice = render_voice(r)
    assert (
        "Ci sono due conversazioni in corso, tutte presidiate, più una voce di rumore di fondo."
        in voice
    )


def test_voice_panoramica_after_urgent_has_no_verb():
    r = _result(
        _entry(gruppo=Gruppo.SUBITO, motivo="bloccato in farmacia", presidio=Presidio.SCOPERTA),
        _entry(gruppo=Gruppo.IN_CORSO, contact_id="c2"),
    )
    voice = render_voice(r)
    assert "Per il resto, una conversazione in corso, presidiata." in voice
    assert "Ci sono" not in voice and "C'è" not in voice


def test_voice_panoramica_never_mismatches_number():
    mismatches = (
        "Ci sono una ",
        "C'è due ",
        "C'è tre ",
        "una conversazioni",
        "una voci",
        "due conversazione in corso",
        "tre conversazione in corso",
        "tutte presidiata",
        "tutte ancora scoperta",
        "una ancora scoperte",
        "due voce di rumore",
    )
    for scoperte in (False, True):
        presidio = Presidio.SCOPERTA if scoperte else Presidio.PRESIDIATA
        for n_corso in range(4):
            for n_rumore in range(4):
                entries = [
                    _entry(gruppo=Gruppo.IN_CORSO, contact_id=f"i{i}", presidio=presidio)
                    for i in range(n_corso)
                ] + [_entry(gruppo=Gruppo.RUMORE, contact_id=f"r{i}") for i in range(n_rumore)]
                voice = render_voice(_result(*entries))
                for bad in mismatches:
                    assert bad not in voice, f"{n_corso}+{n_rumore} scoperte={scoperte}: {voice!r}"


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
