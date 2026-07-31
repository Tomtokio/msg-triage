"""No-network preview of the three renderers.

Builds a realistic :class:`TriageResult` BY HAND (no Callbell, no Anthropic, no
config) and prints the three formats, so the output can be seen the way Telegram
will show it. Deliberately imports ONLY the renderers and the domain types, so it
can never touch the network. Not part of the pytest suite — the assertions live in
``tests/test_renderers.py``; this is the eyeball check. The real-data smoke stays in
``scripts/smoke_triage.py``.

Usage (from the repo root):
    .venv/bin/python scripts/preview_renderers.py
"""

from __future__ import annotations

from msg_triage.renderers import render_all
from msg_triage.triage_engine import (
    ConversationTriage,
    Gruppo,
    Presidio,
    Temperatura,
    TriageResult,
    Urgenza,
)


def _sample_result() -> TriageResult:
    """One SUBITO, two IN CORSO (one scoperta), three RUMORE (two sharing a motivo).

    Exercises: table azione-wins (Verdi, Tramontana), table motivo-fallback (Bianchi,
    every rumore), a scoperta in corso, and rumore dedup (two "conversazione chiusa").
    Bianchi and Miri also carry the client name inside ``motivo`` — the way the model
    writes it live — so the name must appear ONCE per line, and the two rumore voices
    must still merge despite only one of them being signed.
    """
    return TriageResult(
        conversations=(
            ConversationTriage(
                contact_id="c-verdi",
                nome="Verdi",
                gruppo=Gruppo.SUBITO,
                motivo="bloccato in farmacia, la ricetta della **tartaruga** è sbagliata",
                urgenza=Urgenza.ALTA,
                presidio=Presidio.SCOPERTA,
                temperatura=Temperatura.MEDIA,
                stato_sintetico=(
                    "Il signor Verdi è fermo in farmacia: la ricetta della **tartaruga** Ruga è "
                    "sbagliata e non gli danno il farmaco."
                ),
                azione_suggerita="chiamare la farmacia e rifare la ricetta",
                promessa_rilevata=None,
            ),
            ConversationTriage(
                contact_id="c-tramontana",
                nome="Tramontana",
                gruppo=Gruppo.IN_CORSO,
                motivo="chiede se può passare oggi per la **tartaruga**",
                urgenza=Urgenza.MEDIA,
                presidio=Presidio.SCOPERTA,
                temperatura=Temperatura.BASSA,
                stato_sintetico=(
                    "Il signor Tramontana chiede se può passare oggi a prendere la **tartaruga** "
                    "Bianca; nessuno ha ancora risposto."
                ),
                azione_suggerita="rispondere su Bianca e confermare la visita",
                promessa_rilevata=None,
            ),
            ConversationTriage(
                contact_id="c-bianchi",
                nome="Bianchi",
                gruppo=Gruppo.IN_CORSO,
                motivo="La signora Bianchi aspetta conferma per la dimissione del coniglio",
                urgenza=Urgenza.MEDIA,
                presidio=Presidio.PRESIDIATA,
                temperatura=Temperatura.BASSA,
                stato_sintetico=(
                    "La signora Bianchi aspetta conferma per la dimissione del coniglio; le "
                    "colleghe hanno detto entro sera."
                ),
                azione_suggerita="",
                promessa_rilevata=None,
            ),
            ConversationTriage(
                contact_id="c-schanty",
                nome="Schanty",
                gruppo=Gruppo.RUMORE,
                motivo="passante con un pullo di passero, indirizzato alla Lipu",
                urgenza=Urgenza.BASSA,
                presidio=Presidio.PRESIDIATA,
                temperatura=Temperatura.BASSA,
                stato_sintetico=(
                    "Un passante ha trovato un pullo di passero dalla zampa spezzata; "
                    "indirizzato alla Lipu."
                ),
                azione_suggerita="",
                promessa_rilevata=None,
            ),
            ConversationTriage(
                contact_id="c-miri",
                nome="Miri",
                gruppo=Gruppo.RUMORE,
                motivo="Sig.ra Miri: conversazione chiusa",
                urgenza=Urgenza.BASSA,
                presidio=Presidio.PRESIDIATA,
                temperatura=Temperatura.BASSA,
                stato_sintetico="Chiarimento concluso, niente in sospeso.",
                azione_suggerita="",
                promessa_rilevata=None,
            ),
            ConversationTriage(
                contact_id="c-paolo",
                nome="Paolo Tosi",
                gruppo=Gruppo.RUMORE,
                motivo="conversazione chiusa",
                urgenza=Urgenza.BASSA,
                presidio=Presidio.PRESIDIATA,
                temperatura=Temperatura.BASSA,
                stato_sintetico="Chiarimento concluso, niente in sospeso.",
                azione_suggerita="",
                promessa_rilevata=None,
            ),
        )
    )


def main() -> int:
    rendered = render_all(_sample_result())
    for title, text in (
        ("SCHEMA", rendered.schema_text),
        ("TABELLA", rendered.table_text),
        ("VOCALE", rendered.vocal_text),
    ):
        print(f"\n========== {title} ==========\n{text}")
    print(f"\n[vocale: {len(rendered.vocal_text)} caratteri]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
