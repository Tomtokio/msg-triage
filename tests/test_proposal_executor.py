"""Unit test dell'esecutore delle proposte (T10/PR3). Niente rete, niente mock library.

Qui si scrive su record veri di clienti, quindi i test guardano soprattutto tre cose:
che i tag delle colleghe sopravvivano intatti a una PATCH che è un REPLACE, che una
rimozione sia impossibile se il DB non dice che il tag è nostro, e che un fallimento
DOPO la scrittura su Callbell si racconti per quello che è invece di gridare «errore».

Client e store sono finti e iniettati, come altrove; ``now`` è fisso.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from msg_triage.callbell_adapter import CallbellError
from msg_triage.proposal_executor import (
    NO_NAME,
    execute,
    load_deliverable_with_names,
)
from msg_triage.proposal_store import StoredProposal
from msg_triage.proposals import (
    TAG_DA_GESTIRE_SUBITO,
    TAG_DIMISSIONE_OGGI,
    TAG_RICOVERATO,
    Proposal,
    TipoProposta,
)
from msg_triage.storage import SupabaseError

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
ROME_MORNING = datetime(2026, 8, 8, 5, 0, tzinfo=timezone.utc)  # 07:00 Europe/Rome
CONTACT = "c1"
PROPOSAL_ID = "11111111-1111-1111-1111-111111111111"


# --- Fakes ---------------------------------------------------------------------


class FakeClient:
    """Il minimo che l'esecutore tocca di Callbell, con la memoria di cosa è successo."""

    def __init__(self, *, tags=(), name="Gabri92", saved_tags=None, raises=None):
        self.contact = {"uuid": CONTACT, "name": name, "tags": list(tags)}
        self._saved_tags = saved_tags  # per simulare una normalizzazione lato Callbell
        self._raises = raises
        self.reads: list[str] = []
        self.tag_writes: list[list[str]] = []
        self.name_writes: list[str] = []

    def get_contact(self, contact_uuid):
        if self._raises is not None:
            raise self._raises
        self.reads.append(contact_uuid)
        return dict(self.contact)

    def update_contact_tags(self, contact_uuid, tags):
        self.tag_writes.append(list(tags))
        echoed = self._saved_tags if self._saved_tags is not None else list(tags)
        return {"uuid": contact_uuid, "tags": list(echoed)}

    def update_contact_name(self, contact_uuid, name):
        self.name_writes.append(name)
        echoed = self._saved_tags if self._saved_tags is not None else name
        return {"uuid": contact_uuid, "name": echoed}


class FakeStore:
    """ProposalStore finto: registra le scritture e sa fallire dove serve."""

    def __init__(self, *, ours=(), fail_on=None, deliverable=()):
        self._ours = frozenset(ours)
        self._fail_on = fail_on
        self._deliverable = list(deliverable)
        self.recorded: list[tuple] = []
        self.forgotten: list[tuple] = []
        self.inserted: list[list[Proposal]] = []

    def _maybe_fail(self, what):
        if self._fail_on == what:
            raise SupabaseError(f"{what} è giù")

    def system_tags_for(self, contact_ids):
        self._maybe_fail("system_tags_for")
        return {CONTACT: self._ours} if self._ours else {}

    def record_system_tag(self, contact_id, tag, *, proposta_id, applied_at):
        self._maybe_fail("record_system_tag")
        self.recorded.append((contact_id, tag, proposta_id, applied_at))

    def forget_system_tag(self, contact_id, tag):
        self._maybe_fail("forget_system_tag")
        self.forgotten.append((contact_id, tag))

    def insert_pending(self, proposals):
        self._maybe_fail("insert_pending")
        self.inserted.append(list(proposals))
        return []

    def load_deliverable(self, *, now):
        return list(self._deliverable)


def _stored(tipo, payload, motivo="il motivo") -> StoredProposal:
    return StoredProposal(
        id=PROPOSAL_ID,
        proposal=Proposal(contact_id=CONTACT, tipo=tipo, payload=payload, motivo=motivo),
    )


def _run(stored, client, store):
    return execute(stored, client=client, store=store, now=NOW)


# --- tag_add -------------------------------------------------------------------


def test_aggiungere_un_tag_lascia_intatti_quelli_delle_colleghe():
    # La PATCH è un REPLACE: la lista che spediamo È la lista finale. Questo è il test
    # che dice che i tag altrui non spariscono.
    client = FakeClient(tags=["Tommaso rispondi! ", "coniglio"])
    store = FakeStore()

    outcome = _run(_stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO}), client, store)

    assert outcome.ok
    assert client.tag_writes == [["Tommaso rispondi! ", "coniglio", TAG_RICOVERATO]]
    assert store.recorded == [(CONTACT, TAG_RICOVERATO, PROPOSAL_ID, NOW)]


def test_la_lista_scritta_parte_da_una_rilettura_non_dal_triage():
    # Il tap può arrivare ore dopo: si riparte sempre da cosa Callbell ha ADESSO.
    client = FakeClient(tags=["aggiunto da una collega nel frattempo"])
    _run(_stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO}), client, FakeStore())

    assert client.reads == [CONTACT]
    assert client.tag_writes[0][0] == "aggiunto da una collega nel frattempo"


def test_un_tag_gia_presente_non_si_riscrive_ma_diventa_nostro():
    client = FakeClient(tags=[TAG_RICOVERATO])
    store = FakeStore()

    outcome = _run(_stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO}), client, store)

    assert outcome.ok
    assert client.tag_writes == []  # niente da scrivere
    assert store.recorded  # ma da qui in poi è nostro


def test_un_eco_normalizzata_e_una_notizia_non_un_dettaglio():
    client = FakeClient(tags=[], saved_tags=["ricoverato"])  # minuscolo: non è il nostro
    store = FakeStore()

    with pytest.raises(CallbellError):
        _run(_stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO}), client, store)

    assert store.recorded == []  # nessuno stato inventato su una scrittura dubbia


def test_un_tag_fuori_dal_set_chiuso_non_si_scrive_mai():
    client = FakeClient()

    outcome = _run(_stored(TipoProposta.TAG_ADD, {"tag": "urgente"}), client, FakeStore())

    assert not outcome.ok
    assert client.reads == [] and client.tag_writes == []


def test_system_tags_giu_dopo_una_patch_riuscita_racconta_cosa_e_successo():
    # Il tag È sul contatto: dire «errore» manderebbe a cercare una scrittura che c'è.
    client = FakeClient(tags=[])
    store = FakeStore(fail_on="record_system_tag")

    outcome = _run(_stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO}), client, store)

    assert not outcome.ok
    assert "aggiunto" in outcome.message
    assert "system_tags" in outcome.message and "a mano" in outcome.message
    assert client.tag_writes == [[TAG_RICOVERATO]]


# --- follow-up a calendario (nascono alla conferma, non nel run) ---------------


def test_dimissione_oggi_confermata_programma_la_sua_rimozione():
    client = FakeClient(tags=[])
    store = FakeStore()

    _run(
        _stored(
            TipoProposta.TAG_ADD, {"tag": TAG_DIMISSIONE_OGGI, "quando": "2026-08-07"}
        ),
        client,
        store,
    )

    [[followup]] = store.inserted
    assert followup.tipo is TipoProposta.TAG_REMOVE
    assert followup.matures_at == ROME_MORNING


def test_da_gestire_subito_confermato_programma_la_rimozione_a_48_ore():
    store = FakeStore()

    _run(_stored(TipoProposta.TAG_ADD, {"tag": TAG_DA_GESTIRE_SUBITO}), FakeClient(tags=[]), store)

    [[followup]] = store.inserted
    assert followup.matures_at == NOW + timedelta(hours=48)


def test_ricoverato_non_programma_nessuna_rimozione():
    # E quell'assenza È la regola: una degenza lunga con la chat silente tiene il tag.
    store = FakeStore()

    _run(_stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO}), FakeClient(tags=[]), store)

    assert store.inserted == []


def test_un_follow_up_non_scritto_non_annulla_l_azione():
    store = FakeStore(fail_on="insert_pending")

    outcome = _run(
        _stored(TipoProposta.TAG_ADD, {"tag": TAG_DA_GESTIRE_SUBITO}),
        FakeClient(tags=[]),
        store,
    )

    assert outcome.ok  # il tag è sul contatto
    assert "promemoria" in outcome.message  # ma non in silenzio


# --- tag_remove ----------------------------------------------------------------


def test_non_si_toglie_un_tag_che_il_database_non_dice_nostro():
    # Invariante n.1 di T10, ricontrollata al momento della scrittura: `Ricoverato` è
    # byte-identico a quello che mettono le colleghe, il nome non distingue niente.
    client = FakeClient(tags=[TAG_RICOVERATO])
    store = FakeStore(ours=())

    outcome = _run(_stored(TipoProposta.TAG_REMOVE, {"tag": TAG_RICOVERATO}), client, store)

    assert not outcome.ok
    assert client.reads == [] and client.tag_writes == []
    assert store.forgotten == []


def test_togliere_un_tag_nostro_lascia_in_piedi_tutti_gli_altri():
    client = FakeClient(tags=["coniglio", TAG_RICOVERATO, "Tommaso rispondi! "])
    store = FakeStore(ours=[TAG_RICOVERATO])

    outcome = _run(_stored(TipoProposta.TAG_REMOVE, {"tag": TAG_RICOVERATO}), client, store)

    assert outcome.ok
    assert client.tag_writes == [["coniglio", "Tommaso rispondi! "]]
    assert store.forgotten == [(CONTACT, TAG_RICOVERATO)]


def test_un_tag_gia_sparito_da_callbell_si_dimentica_e_basta():
    client = FakeClient(tags=["coniglio"])
    store = FakeStore(ours=[TAG_RICOVERATO])

    outcome = _run(_stored(TipoProposta.TAG_REMOVE, {"tag": TAG_RICOVERATO}), client, store)

    assert outcome.ok
    assert client.tag_writes == []
    assert store.forgotten == [(CONTACT, TAG_RICOVERATO)]


def test_system_tags_giu_dopo_una_rimozione_riuscita_lo_dice():
    client = FakeClient(tags=[TAG_RICOVERATO])
    store = FakeStore(ours=[TAG_RICOVERATO], fail_on="forget_system_tag")

    outcome = _run(_stored(TipoProposta.TAG_REMOVE, {"tag": TAG_RICOVERATO}), client, store)

    assert not outcome.ok
    assert "tolto" in outcome.message and "a mano" in outcome.message


def test_una_lettura_di_system_tags_che_fallisce_prima_della_scrittura_risale():
    # Prima della PATCH non c'è niente da raccontare: è un fallimento e basta.
    store = FakeStore(ours=[TAG_RICOVERATO], fail_on="system_tags_for")

    with pytest.raises(SupabaseError):
        _run(_stored(TipoProposta.TAG_REMOVE, {"tag": TAG_RICOVERATO}), FakeClient(), store)


# --- rename --------------------------------------------------------------------


def test_la_rinomina_scrive_il_nome_e_verifica_l_eco():
    client = FakeClient(name="Gabri92")

    outcome = _run(
        _stored(TipoProposta.RENAME, {"nome": "Mario Rossi coniglio Asio"}), client, FakeStore()
    )

    assert outcome.ok
    assert client.name_writes == ["Mario Rossi coniglio Asio"]
    # Nessuna rilettura: `name` è un valore assoluto, non ha collaterali da perdere.
    assert client.reads == []


def test_un_nome_normalizzato_da_callbell_e_un_errore():
    client = FakeClient(saved_tags="Mario Rossi")  # ha tagliato la coda

    with pytest.raises(CallbellError):
        _run(_stored(TipoProposta.RENAME, {"nome": "Mario Rossi coniglio Asio"}), client, FakeStore())


def test_una_rinomina_senza_nome_non_scrive_niente():
    client = FakeClient()

    outcome = _run(_stored(TipoProposta.RENAME, {}), client, FakeStore())

    assert not outcome.ok
    assert client.name_writes == []


# --- consegna: le domande con il nome di adesso --------------------------------


def test_la_consegna_accoppia_ogni_proposta_al_nome_corrente_del_contatto():
    stored = _stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO})
    client = FakeClient(name="Bonifazi")

    [(item, nome)] = load_deliverable_with_names(
        FakeStore(deliverable=[stored]), client, now=NOW
    )

    assert item is stored
    assert nome == "Bonifazi"


def test_un_contatto_senza_nome_resta_una_frase_leggibile():
    client = FakeClient(name="")

    [(_, nome)] = load_deliverable_with_names(
        FakeStore(deliverable=[_stored(TipoProposta.RENAME, {"nome": "Mario Rossi"})]),
        client,
        now=NOW,
    )

    assert nome == NO_NAME


def test_un_contatto_illeggibile_salta_la_consegna_invece_di_farla_cadere():
    # La riga resta non consegnata e torna al run dopo: meglio che una domanda monca.
    client = FakeClient(raises=CallbellError("500"))

    assert (
        load_deliverable_with_names(
            FakeStore(deliverable=[_stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO})]),
            client,
            now=NOW,
        )
        == []
    )


def test_un_eco_riordinata_non_e_un_errore():
    # `tags` è un insieme assoluto anche per Callbell e nessuno legge una posizione:
    # far fallire una scrittura riuscita sarebbe l'errore peggiore dei due.
    client = FakeClient(tags=["coniglio"], saved_tags=[TAG_RICOVERATO, "coniglio"])
    store = FakeStore()

    outcome = _run(_stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO}), client, store)

    assert outcome.ok
    assert store.recorded


def test_un_tag_perso_nell_eco_e_un_errore():
    # Qui invece manca davvero qualcosa: il tag della collega non è tornato indietro.
    client = FakeClient(tags=["coniglio"], saved_tags=[TAG_RICOVERATO])

    with pytest.raises(CallbellError):
        _run(_stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO}), client, FakeStore())


def test_un_riordino_si_accetta_ma_non_in_silenzio(caplog):
    # Che Callbell riordini non è mai stato verificato: la pulizia del 2026-08-04
    # toglieva soltanto. Ogni scrittura vera diventa un probe su quell'assunzione.
    client = FakeClient(tags=["coniglio"], saved_tags=[TAG_RICOVERATO, "coniglio"])

    with caplog.at_level("WARNING", logger="msg_triage.proposal_executor"):
        outcome = _run(_stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO}), client, FakeStore())

    assert outcome.ok
    assert "riordinato" in caplog.text
    assert CONTACT in caplog.text


def test_un_eco_nell_ordine_giusto_non_logga_niente(caplog):
    client = FakeClient(tags=["coniglio"])

    with caplog.at_level("WARNING", logger="msg_triage.proposal_executor"):
        _run(_stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO}), client, FakeStore())

    assert caplog.records == []


def test_un_duplicato_collassato_nell_eco_e_un_errore():
    # Il confronto è fra multiset, non fra set: con set() questo passerebbe e
    # l'invariante «nessun tag perso» direbbe il falso.
    client = FakeClient(
        tags=["coniglio", "coniglio"], saved_tags=["coniglio", TAG_RICOVERATO]
    )

    with pytest.raises(CallbellError):
        _run(_stored(TipoProposta.TAG_ADD, {"tag": TAG_RICOVERATO}), client, FakeStore())
