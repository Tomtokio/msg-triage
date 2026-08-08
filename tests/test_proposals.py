"""Unit tests for the T10 deterministic rules. Pure: no network, no DB, no real clock.

``now`` is injected everywhere, so nothing here depends on the day the suite runs — which
matters more than usual, because half these rules are about dates.

What these tests CANNOT show, and it is the real risk of PR2: that the model produces the
values the rules expect. ``ricovero`` and ``dimissione`` have never been exercised on real
data. That is what ``scripts/smoke_triage.py --facts --proposals`` is for.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from msg_triage.proposals import (
    TAG_DA_GESTIRE_SUBITO,
    TAG_DIMISSIONE_OGGI,
    TAG_RICOVERATO,
    Decision,
    Proposal,
    TipoProposta,
    build_proposals,
    followups_for,
)
from msg_triage.source_adapter import Conversation
from msg_triage.triage_engine import (
    Animale,
    ConversationTriage,
    Dimissione,
    Fatti,
    Gruppo,
    Presidio,
    Ricovero,
    StatoDimissione,
    Temperatura,
    Urgenza,
)

ROME = ZoneInfo("Europe/Rome")
# 12:00 UTC = 14:00 in Rome, comfortably inside the same day on both clocks.
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
TODAY = "2026-08-07"
CONTACT = "cb-1"


# --- Builders ------------------------------------------------------------------


def _fatti(**over) -> Fatti:
    base = dict(
        ricovero=Ricovero.NON_MENZIONATO,
        dimissione=None,
        animali=(),
        proprietario=None,
    )
    base.update(over)
    return Fatti(**base)


def _entry(**over) -> ConversationTriage:
    base = dict(
        contact_id=CONTACT,
        nome="Gabri92",
        gruppo=Gruppo.IN_CORSO,
        motivo="chiede la ricetta",
        urgenza=Urgenza.MEDIA,
        presidio=Presidio.PRESIDIATA,
        temperatura=Temperatura.BASSA,
        stato_sintetico="Chiede la ricetta.",
        azione_suggerita="",
        promessa_rilevata=None,
        fatti=_fatti(),
    )
    base.update(over)
    return ConversationTriage(**base)


def _convo(entry: ConversationTriage, *, tags=()) -> Conversation:
    """The neutral conversation behind an entry: same id and name, by construction."""
    return Conversation(
        contact_id=entry.contact_id,
        name=entry.nome,
        channel="whatsapp",
        tags=tuple(tags),
        assigned_user=None,
        messages=(),
    )


def _run(
    *, tags=(), name="Gabri92", system_tags=None, decisions=(), now=NOW, **over
) -> list[Proposal]:
    """One conversation through the rules; ``over`` goes to the triage entry."""
    entry = _entry(nome=name, **over)
    return build_proposals(
        [entry],
        [_convo(entry, tags=tags)],
        system_tags=system_tags,
        decisions=decisions,
        now=now,
    )


def _added(proposals: list[Proposal]) -> list[str]:
    return [p.payload["tag"] for p in proposals if p.tipo is TipoProposta.TAG_ADD]


def _removed(proposals: list[Proposal]) -> list[str]:
    return [p.payload["tag"] for p in proposals if p.tipo is TipoProposta.TAG_REMOVE]


def _ours(*tags: str) -> dict[str, frozenset[str]]:
    return {CONTACT: frozenset(tags)}


def _decision(tipo: str, tag: str, stato: str, **over) -> Decision:
    return Decision(contact_id=CONTACT, tipo=tipo, tag=tag, stato=stato, **over)


# --- Ricoverato ----------------------------------------------------------------


def test_un_ricovero_in_corso_propone_il_tag():
    proposals = _run(fatti=_fatti(ricovero=Ricovero.IN_CORSO))

    assert _added(proposals) == [TAG_RICOVERATO]
    assert proposals[0].matures_at is None  # immediata, non programmata
    assert proposals[0].motivo


def test_il_tag_gia_presente_non_si_ripropone():
    assert _run(fatti=_fatti(ricovero=Ricovero.IN_CORSO), tags=(TAG_RICOVERATO,)) == []


def test_il_confronto_coi_tag_del_contatto_e_byte_per_byte():
    # Una variante minuscola di una collega NON è il nostro tag: si propone lo stesso.
    proposals = _run(fatti=_fatti(ricovero=Ricovero.IN_CORSO), tags=("ricoverato",))

    assert _added(proposals) == [TAG_RICOVERATO]


def test_nessuna_rimozione_senza_la_riga_in_system_tags():
    # Invariante 1: un `Ricoverato` che non abbiamo messo noi la regola semantica non lo
    # tocca mai, nemmeno quando i fatti dicono che il ricovero è finito.
    assert _run(fatti=_fatti(ricovero=Ricovero.CONCLUSO), tags=(TAG_RICOVERATO,)) == []


def test_il_ricovero_concluso_propone_la_rimozione_subito():
    proposals = _run(
        fatti=_fatti(ricovero=Ricovero.CONCLUSO),
        tags=(TAG_RICOVERATO,),
        system_tags=_ours(TAG_RICOVERATO),
    )

    assert _removed(proposals) == [TAG_RICOVERATO]
    assert proposals[0].matures_at is None


def test_anche_una_dimissione_avvenuta_propone_la_rimozione_subito():
    proposals = _run(
        fatti=_fatti(dimissione=Dimissione(StatoDimissione.AVVENUTA, "2026-08-06")),
        tags=(TAG_RICOVERATO,),
        system_tags=_ours(TAG_RICOVERATO),
    )

    assert _removed(proposals) == [TAG_RICOVERATO]


def test_una_dimissione_fissata_programma_la_rimozione_al_mattino_dopo():
    proposals = _run(
        fatti=_fatti(
            ricovero=Ricovero.IN_CORSO,
            dimissione=Dimissione(StatoDimissione.FISSATA, "2026-08-10"),
        ),
        tags=(TAG_RICOVERATO,),
        system_tags=_ours(TAG_RICOVERATO),
    )

    [removal] = [p for p in proposals if p.tipo is TipoProposta.TAG_REMOVE]
    assert removal.matures_at == datetime(2026, 8, 11, 7, 0, tzinfo=ROME)


def test_ricoverato_non_viene_mai_rimosso_a_tempo():
    # Una degenza lunga con la chat silente non deve perdere il tag: `non_menzionato`
    # non è "dimesso", ed è esattamente per questo che il campo ha tre valori.
    assert (
        _run(
            fatti=_fatti(ricovero=Ricovero.NON_MENZIONATO),
            tags=(TAG_RICOVERATO,),
            system_tags=_ours(TAG_RICOVERATO),
        )
        == []
    )


def test_una_data_di_dimissione_fuori_scala_non_programma_niente():
    # Una data lontanissima non significa "dimissione lontana": significa che il campo
    # non è quello che crediamo. Nessuna proposta batte una proposta che matura nel 2027.
    assert (
        _run(
            fatti=_fatti(
                ricovero=Ricovero.IN_CORSO,
                dimissione=Dimissione(StatoDimissione.FISSATA, "2025-01-01"),
            ),
            tags=(TAG_RICOVERATO,),
            system_tags=_ours(TAG_RICOVERATO),
        )
        == []
    )


# --- Dimissione oggi -----------------------------------------------------------


def test_una_dimissione_di_oggi_propone_il_tag_con_la_data_nel_payload():
    proposals = _run(fatti=_fatti(dimissione=Dimissione(StatoDimissione.FISSATA, TODAY)))

    [add] = proposals
    # `quando` viaggia perché la scadenza del follow-up si calcola dalla dimissione,
    # non da quando capita il tap.
    assert add.payload == {"tag": TAG_DIMISSIONE_OGGI, "quando": TODAY}


def test_una_dimissione_di_domani_non_propone_il_tag_di_oggi():
    assert _run(fatti=_fatti(dimissione=Dimissione(StatoDimissione.FISSATA, "2026-08-08"))) == []


def test_oggi_e_quello_di_roma_non_quello_di_utc():
    # 23:30 UTC del 7 sono già l'1:30 dell'8 a Roma, e questa è una regola same-day:
    # è il caso preciso per cui la data locale sta dentro il blocco fatti.
    late = datetime(2026, 8, 7, 23, 30, tzinfo=timezone.utc)

    domani_utc = _run(
        fatti=_fatti(dimissione=Dimissione(StatoDimissione.FISSATA, "2026-08-08")), now=late
    )
    oggi_utc = _run(
        fatti=_fatti(dimissione=Dimissione(StatoDimissione.FISSATA, "2026-08-07")), now=late
    )

    assert _added(domani_utc) == [TAG_DIMISSIONE_OGGI]
    assert oggi_utc == []


# --- Da gestire subito ---------------------------------------------------------


def test_il_gruppo_subito_propone_il_tag():
    assert _added(_run(gruppo=Gruppo.SUBITO)) == [TAG_DA_GESTIRE_SUBITO]


def test_da_gestire_subito_vive_anche_senza_fatti():
    # `fatti=None` (payload malformato, PR1) costa i suoi fatti e basta: non deve
    # costare anche una regola che i fatti non li ha mai letti.
    assert _added(_run(gruppo=Gruppo.SUBITO, fatti=None)) == [TAG_DA_GESTIRE_SUBITO]


def test_senza_fatti_nessuna_regola_fact_driven():
    assert _run(fatti=None) == []


# --- Rinomina ------------------------------------------------------------------


def test_nome_povero_e_un_animale_compongono_il_template():
    proposals = _run(
        name="Gabri92",
        fatti=_fatti(
            proprietario="Gabriele Di Resta",
            animali=(Animale("parrocchetto", "Saetta"),),
        ),
    )

    [rename] = proposals
    assert rename.tipo is TipoProposta.RENAME
    assert rename.payload == {"nome": "Gabriele Di Resta parrocchetto Saetta"}


def test_piu_animali_lasciano_solo_il_proprietario():
    proposals = _run(
        name="Gabri92",
        fatti=_fatti(
            proprietario="Mario Rossi",
            animali=(Animale("coniglio", "Bunny"), Animale("canarino", "Titti")),
        ),
    )

    assert proposals[0].payload == {"nome": "Mario Rossi"}


def test_con_informazione_parziale_si_propone_il_parziale():
    proposals = _run(name="Ale", fatti=_fatti(proprietario="Alessandra Neri"))

    assert proposals[0].payload == {"nome": "Alessandra Neri"}


def test_un_nome_gia_utilizzabile_non_si_tocca():
    # Due parole, niente cifre: non è un default di WhatsApp, e senza il secondo
    # trigger (semplificazione) non c'è motivo di proporre niente.
    assert _run(name="Mario Rossi", fatti=_fatti(proprietario="Luca Bianchi")) == []


def test_un_contatto_gia_rinominato_non_riceve_la_stessa_proposta():
    assert (
        _run(
            name="Mario Rossi coniglio Bunny",
            fatti=_fatti(proprietario="Mario Rossi", animali=(Animale("coniglio", "Bunny"),)),
        )
        == []
    )


def test_il_secondo_animale_fa_semplificare_un_nome_non_povero():
    proposals = _run(
        name="Mario Rossi coniglio Bunny",
        fatti=_fatti(
            proprietario="Mario Rossi",
            animali=(Animale("coniglio", "Bunny"), Animale("canarino", "Titti")),
        ),
    )

    [rename] = proposals
    assert rename.payload == {"nome": "Mario Rossi"}
    assert rename.motivo == "ha più animali: Bunny, Titti"


def test_un_proprietario_che_riecheggia_il_nome_del_contatto_non_produce_niente():
    # Il prompt vieta di leggere il proprietario dall'intestazione del contatto (che è
    # proprio ciò che T10 vuole correggere). Questa è la rete deterministica.
    assert (
        _run(name="Bonucci", fatti=_fatti(proprietario="bonucci", animali=(Animale("cane", "Zac"),)))
        == []
    )


@pytest.mark.parametrize(
    "proprietario",
    [
        None,
        "",
        "   ",
        "Mario 2",  # cifre: nessun nome di persona le porta
        "il proprietario del coniglio bianco che chiama sempre",  # prosa, non un nome
    ],
)
def test_un_proprietario_non_plausibile_non_produce_niente(proprietario):
    assert _run(name="Ale", fatti=_fatti(proprietario=proprietario)) == []


def test_un_animale_non_plausibile_resta_fuori_dal_nome():
    proposals = _run(
        name="Ale",
        fatti=_fatti(
            proprietario="Alessandra Neri",
            animali=(Animale("non specificato al momento", "12"),),
        ),
    )

    assert proposals[0].payload == {"nome": "Alessandra Neri"}


# --- Idempotenza ---------------------------------------------------------------


def test_una_proposta_identica_gia_pending_non_si_duplica():
    decisions = (_decision("tag_add", TAG_RICOVERATO, "pending"),)

    assert _run(fatti=_fatti(ricovero=Ricovero.IN_CORSO), decisions=decisions) == []


def test_una_rinomina_rifiutata_non_torna_mai_piu():
    decisions = (
        _decision("rename", "", "rifiutata", decided_at=NOW - timedelta(days=400)),
    )

    assert _run(name="Ale", fatti=_fatti(proprietario="Alessandra Neri"), decisions=decisions) == []


def test_un_tag_rifiutato_torna_dopo_trenta_giorni():
    fatti = _fatti(ricovero=Ricovero.IN_CORSO)

    recente = (_decision("tag_add", TAG_RICOVERATO, "rifiutata", decided_at=NOW - timedelta(days=10)),)
    vecchia = (_decision("tag_add", TAG_RICOVERATO, "rifiutata", decided_at=NOW - timedelta(days=40)),)

    assert _run(fatti=fatti, decisions=recente) == []
    assert _added(_run(fatti=fatti, decisions=vecchia)) == [TAG_RICOVERATO]


def test_la_riproponibilita_si_conta_dalla_decisione_non_dalla_proposta():
    # 40 giorni in coda, 10 dal rifiuto: è ancora un rifiuto. Con `created_at` questo
    # tag tornerebbe un mese prima del dovuto — la ragione per cui esiste `decided_at`.
    decisions = (
        _decision(
            "tag_add",
            TAG_RICOVERATO,
            "rifiutata",
            created_at=NOW - timedelta(days=40),
            decided_at=NOW - timedelta(days=10),
        ),
    )

    assert _run(fatti=_fatti(ricovero=Ricovero.IN_CORSO), decisions=decisions) == []


def test_un_rifiuto_senza_data_non_si_ripropone():
    decisions = (_decision("tag_add", TAG_RICOVERATO, "rifiutata"),)

    assert _run(fatti=_fatti(ricovero=Ricovero.IN_CORSO), decisions=decisions) == []


def test_una_proposta_eseguita_non_blocca():
    decisions = (_decision("tag_add", TAG_RICOVERATO, "eseguita", decided_at=NOW),)

    assert _added(_run(fatti=_fatti(ricovero=Ricovero.IN_CORSO), decisions=decisions)) == [
        TAG_RICOVERATO
    ]


def test_il_rifiuto_di_un_tag_non_ne_blocca_un_altro():
    decisions = (_decision("tag_add", TAG_RICOVERATO, "rifiutata", decided_at=NOW),)

    proposals = _run(
        gruppo=Gruppo.SUBITO, fatti=_fatti(ricovero=Ricovero.IN_CORSO), decisions=decisions
    )

    assert _added(proposals) == [TAG_DA_GESTIRE_SUBITO]


# --- Follow-up (li chiamerà l'executor in PR3) ---------------------------------


def test_il_follow_up_di_dimissione_oggi_matura_il_mattino_dopo_la_dimissione():
    add = Proposal(
        CONTACT,
        TipoProposta.TAG_ADD,
        {"tag": TAG_DIMISSIONE_OGGI, "quando": "2026-08-07"},
        "la dimissione è fissata per oggi",
    )

    [removal] = followups_for(add, now=NOW)

    assert removal.tipo is TipoProposta.TAG_REMOVE
    assert removal.payload == {"tag": TAG_DIMISSIONE_OGGI}
    assert removal.matures_at == datetime(2026, 8, 8, 7, 0, tzinfo=ROME)


def test_il_follow_up_di_da_gestire_subito_matura_a_quarantotto_ore():
    add = Proposal(CONTACT, TipoProposta.TAG_ADD, {"tag": TAG_DA_GESTIRE_SUBITO}, "…")

    [removal] = followups_for(add, now=NOW)

    assert removal.matures_at == NOW + timedelta(hours=48)


def test_ricoverato_non_ha_follow_up():
    # L'assenza È la regola: un ricovero finisce quando lo dicono i messaggi, mai
    # perché è passato del tempo.
    add = Proposal(CONTACT, TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO}, "…")

    assert followups_for(add, now=NOW) == ()


def test_una_rimozione_non_genera_altri_follow_up():
    remove = Proposal(CONTACT, TipoProposta.TAG_REMOVE, {"tag": TAG_DIMISSIONE_OGGI}, "…")

    assert followups_for(remove, now=NOW) == ()


# --- Più conversazioni ---------------------------------------------------------


def test_ogni_conversazione_porta_le_sue_proposte():
    uno = _entry(contact_id="cb-1", nome="Ale", gruppo=Gruppo.SUBITO)
    due = _entry(contact_id="cb-2", nome="Bianchi", fatti=_fatti(ricovero=Ricovero.IN_CORSO))

    proposals = build_proposals(
        [uno, due], [_convo(uno), _convo(due)], system_tags={}, decisions=(), now=NOW
    )

    assert {(p.contact_id, p.payload["tag"]) for p in proposals} == {
        ("cb-1", TAG_DA_GESTIRE_SUBITO),
        ("cb-2", TAG_RICOVERATO),
    }


def test_una_voce_senza_conversazione_viene_saltata_senza_far_cadere_le_altre():
    # Non può succedere (il contact_id viene dalla sorgente), ma se succedesse deve
    # costare quella conversazione, non il run.
    orfana = _entry(contact_id="cb-orfana", gruppo=Gruppo.SUBITO)
    buona = _entry(contact_id="cb-2", nome="Bianchi", gruppo=Gruppo.SUBITO)

    proposals = build_proposals(
        [orfana, buona], [_convo(buona)], system_tags={}, decisions=(), now=NOW
    )

    assert [p.contact_id for p in proposals] == ["cb-2"]
