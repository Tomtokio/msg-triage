"""Unit tests for the T10 proposal store. No network, no mock library.

Same dependency-injection style as tests/test_storage.py: a hand-rolled fake session at
the HTTP boundary. Three behaviours carry the weight here, and each has its own test —
the reads reach the custom schema (``Accept-Profile``, the header T7 never needed), the
rows go out as ``pending`` with client-generated ids, and **failures propagate**, which
is the exact opposite of ``save_triage_run``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import pytest
import requests

from msg_triage import proposal_store
from msg_triage.config import Config, load_config
from msg_triage.proposal_store import (
    ProposalStore,
    build_and_store_proposals,
    proposals_enabled,
)
from msg_triage.proposals import (
    TAG_DIMISSIONE_OGGI,
    TAG_RICOVERATO,
    Proposal,
    TipoProposta,
)
from msg_triage.source_adapter import Conversation
from msg_triage.storage import SCHEMA, SupabaseError, SupabaseStore
from msg_triage.triage_engine import (
    ConversationTriage,
    Fatti,
    Gruppo,
    Presidio,
    Ricovero,
    Temperatura,
    TriageResult,
    Urgenza,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
IDS = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]

_BASE_ENV = {
    "CALLBELL_API_KEY": "cb-key",
    "ANTHROPIC_API_KEY": "an-key",
    "TELEGRAM_BOT_TOKEN": "123456:ABC-fake-token",
    "TELEGRAM_ALLOWED_USER_ID": "123456789",
}


@pytest.fixture(autouse=True)
def _reset_one_shot_warning(monkeypatch):
    """The "flag on, Supabase off" warning fires once per process; reset it per test."""
    monkeypatch.setattr(proposal_store, "_WARNED_WITHOUT_SUPABASE", False)


def _config(
    *, url: str = "https://demo.supabase.co", key: str = "eyJh-fake", proposals: str = "true"
) -> Config:
    return load_config(
        {**_BASE_ENV, "SUPABASE_URL": url, "SUPABASE_KEY": key, "ENABLE_PROPOSALS": proposals}
    )


# --- Fakes ---------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeSession:
    """Records every call and returns queued responses in order."""

    def __init__(self, responses=None, raises=None):
        self._responses = list(responses or [])
        self._raises = raises
        self.calls: list[dict] = []

    def _record(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._raises is not None:
            raise self._raises
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse(payload=[])

    def get(self, url, **kwargs):
        return self._record("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._record("POST", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._record("PATCH", url, **kwargs)


def _store(session: FakeSession) -> ProposalStore:
    config = _config()
    return ProposalStore(SupabaseStore(config.supabase_url, config.supabase_key, session=session))


def _ids():
    """Deterministic client-side ids, in order."""
    remaining = list(IDS)
    return lambda: remaining.pop(0)


# --- Domain builders -----------------------------------------------------------


def _entry(contact_id: str = "cb-1", **over) -> ConversationTriage:
    base = dict(
        contact_id=contact_id,
        nome="Gabri92",
        gruppo=Gruppo.IN_CORSO,
        motivo="chiede la ricetta",
        urgenza=Urgenza.MEDIA,
        presidio=Presidio.PRESIDIATA,
        temperatura=Temperatura.BASSA,
        stato_sintetico="Chiede la ricetta.",
        azione_suggerita="",
        promessa_rilevata=None,
        fatti=Fatti(
            ricovero=Ricovero.IN_CORSO, dimissione=None, animali=(), proprietario=None
        ),
    )
    base.update(over)
    return ConversationTriage(**base)


def _convo(contact_id: str = "cb-1", *, tags=()) -> Conversation:
    return Conversation(
        contact_id=contact_id,
        name="Gabri92",
        channel="whatsapp",
        tags=tuple(tags),
        assigned_user=None,
        messages=(),
    )


def _proposal(**over) -> Proposal:
    base = dict(
        contact_id="cb-1",
        tipo=TipoProposta.TAG_ADD,
        payload={"tag": TAG_RICOVERATO},
        motivo="dai messaggi risulta un ricovero in corso",
        matures_at=None,
    )
    base.update(over)
    return Proposal(**base)


# --- insert_pending ------------------------------------------------------------


def test_insert_pending_scrive_tutto_in_un_solo_post():
    session = FakeSession()

    stored = _store(session).insert_pending(
        [_proposal(), _proposal(payload={"tag": TAG_DIMISSIONE_OGGI})], new_id=_ids()
    )

    assert len(session.calls) == 1  # in blocco, non una chiamata per proposta
    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"].endswith("/rest/v1/proposals")
    assert [item.id for item in stored] == IDS


def test_la_riga_nasce_pending_e_senza_created_at():
    session = FakeSession()

    _store(session).insert_pending([_proposal()], new_id=_ids())

    [row] = session.calls[0]["json"]
    assert row["id"] == IDS[0]  # id dal client: nessuna riga da rileggere
    assert row["contact_id"] == "cb-1"
    assert row["tipo"] == "tag_add"
    assert row["payload"] == {"tag": TAG_RICOVERATO}
    assert row["motivo"] == "dai messaggi risulta un ricovero in corso"
    assert row["stato"] == "pending"
    assert row["matures_at"] is None
    assert "created_at" not in row  # lasciato al default del database
    assert "decided_at" not in row  # NULL finché non si tappa (PR3)


def test_una_proposta_programmata_porta_matures_at_in_iso():
    session = FakeSession()
    matura = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)

    _store(session).insert_pending([_proposal(matures_at=matura)], new_id=_ids())

    assert session.calls[0]["json"][0]["matures_at"] == matura.isoformat()


def test_insert_pending_senza_proposte_non_chiama_niente():
    session = FakeSession()

    assert _store(session).insert_pending([]) == []
    assert session.calls == []


# --- Le letture: gli header dello schema custom --------------------------------


def test_la_get_manda_accept_profile():
    # Senza Accept-Profile PostgREST cerca in `public` e fallisce in un modo che
    # sembra un grant mancante. T7 non se ne era mai accorto: non leggeva.
    session = FakeSession()

    _store(session).system_tags_for(["cb-1"])

    headers = session.calls[0]["headers"]
    assert headers["Accept-Profile"] == SCHEMA
    assert "Prefer" not in headers  # Prefer governa cosa torna da una SCRITTURA
    assert session.calls[0]["timeout"] > 0


def test_il_post_manda_content_profile_e_prefer():
    session = FakeSession()

    _store(session).insert_pending([_proposal()], new_id=_ids())

    headers = session.calls[0]["headers"]
    assert headers["Content-Profile"] == SCHEMA
    assert headers["Prefer"] == "return=minimal"


# --- load_decisions ------------------------------------------------------------


def test_load_decisions_chiede_solo_gli_stati_che_contano():
    session = FakeSession()

    _store(session).load_decisions(["cb-1", "cb-2", "cb-1"])

    params = session.calls[0]["params"]
    assert params["contact_id"] == 'in.("cb-1","cb-2")'  # deduplicati
    assert params["stato"] == 'in.("pending","rifiutata")'
    assert params["order"] == "created_at.desc"


def test_load_decisions_mappa_le_righe_nel_dominio():
    rows = [
        {
            "contact_id": "cb-1",
            "tipo": "tag_add",
            "payload": {"tag": TAG_RICOVERATO},
            "stato": "rifiutata",
            "created_at": "2026-07-01T10:00:00+00:00",
            "decided_at": "2026-07-02T09:30:00+00:00",
        },
        {
            "contact_id": "cb-2",
            "tipo": "rename",
            "payload": {"nome": "Mario Rossi"},
            "stato": "pending",
            "created_at": "2026-08-01T10:00:00+00:00",
            "decided_at": None,
        },
    ]
    session = FakeSession(responses=[FakeResponse(payload=rows)])

    tag_decision, rename_decision = _store(session).load_decisions(["cb-1", "cb-2"])

    assert tag_decision.key == ("cb-1", "tag_add", TAG_RICOVERATO)
    assert tag_decision.when == datetime(2026, 7, 2, 9, 30, tzinfo=timezone.utc)
    # Una rinomina è identificata dal CONTATTO, non dal nome proposto.
    assert rename_decision.key == ("cb-2", "rename", "")
    assert rename_decision.when == datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)


def test_una_data_illeggibile_diventa_none_invece_di_far_cadere_il_run():
    rows = [
        {
            "contact_id": "cb-1",
            "tipo": "tag_add",
            "payload": {"tag": TAG_RICOVERATO},
            "stato": "rifiutata",
            "created_at": "non una data",
            "decided_at": None,
        }
    ]
    session = FakeSession(responses=[FakeResponse(payload=rows)])

    [decision] = _store(session).load_decisions(["cb-1"])

    assert decision.when is None


def test_senza_contatti_non_si_interroga_il_database():
    session = FakeSession()
    store = _store(session)

    assert store.load_decisions([]) == ()
    assert store.system_tags_for([]) == {}
    assert session.calls == []


# --- system_tags_for -----------------------------------------------------------


def test_system_tags_for_raggruppa_per_contatto():
    rows = [
        {"contact_id": "cb-1", "tag": TAG_RICOVERATO},
        {"contact_id": "cb-1", "tag": TAG_DIMISSIONE_OGGI},
        {"contact_id": "cb-2", "tag": TAG_RICOVERATO},
    ]
    session = FakeSession(responses=[FakeResponse(payload=rows)])

    ours = _store(session).system_tags_for(["cb-1", "cb-2", "cb-3"])

    assert ours == {
        "cb-1": frozenset({TAG_RICOVERATO, TAG_DIMISSIONE_OGGI}),
        "cb-2": frozenset({TAG_RICOVERATO}),
    }
    # cb-3 semplicemente non c'è: non porta nessun tag nostro, qualunque cosa mostri
    # la sua lista su Callbell.
    assert "cb-3" not in ours


# --- Gli errori NON sono silenziosi --------------------------------------------


def test_un_errore_http_si_propaga():
    # L'opposto di save_triage_run: una proposta che non possiamo ricordare di aver
    # fatto non deve esistere, quindi il fallimento arriva a chi ha chiamato.
    session = FakeSession(responses=[FakeResponse(status_code=400, payload={"code": "42P01"})])

    with pytest.raises(SupabaseError):
        _store(session).insert_pending([_proposal()], new_id=_ids())


def test_un_errore_di_rete_si_propaga():
    session = FakeSession(raises=requests.ConnectionError("connessione rifiutata"))

    with pytest.raises(SupabaseError):
        _store(session).load_decisions(["cb-1"])


def test_un_corpo_che_non_e_una_lista_e_un_errore_non_zero_righe():
    # Una pagina di errore di un proxy non deve passare per "nessuna riga".
    session = FakeSession(responses=[FakeResponse(payload={"message": "Bad gateway"})])

    with pytest.raises(SupabaseError):
        _store(session).system_tags_for(["cb-1"])


# --- Il gate -------------------------------------------------------------------


def test_proposals_enabled_richiede_il_flag():
    assert proposals_enabled(_config(proposals="false")) is False


def test_proposals_enabled_richiede_anche_supabase(caplog):
    with caplog.at_level(logging.WARNING, logger="msg_triage.proposal_store"):
        assert proposals_enabled(_config(url="unused")) is False
        assert proposals_enabled(_config(url="unused")) is False

    # Una volta sola: è una configurazione da sistemare, non una lagna per ogni triage.
    assert caplog.text.count("ENABLE_PROPOSALS") == 1


def test_proposals_enabled_con_flag_e_supabase():
    assert proposals_enabled(_config()) is True


# --- build_and_store_proposals -------------------------------------------------


def test_col_flag_spento_non_si_costruisce_nemmeno_una_sessione(monkeypatch):
    built: list[int] = []
    monkeypatch.setattr(requests, "Session", lambda: built.append(1))
    result = TriageResult(conversations=(_entry(),))

    assert build_and_store_proposals(_config(proposals="false"), result, [_convo()]) == []
    assert built == []


def test_il_giro_completo_legge_poi_scrive():
    session = FakeSession(
        responses=[
            FakeResponse(payload=[]),  # system_tags_for
            FakeResponse(payload=[]),  # load_decisions
            FakeResponse(status_code=201, payload=None),  # insert
        ]
    )
    result = TriageResult(conversations=(_entry(),))

    stored = build_and_store_proposals(
        _config(),
        result,
        [_convo()],
        store=_store(session),
        now=NOW,
        new_id=_ids(),
    )

    assert [call["method"] for call in session.calls] == ["GET", "GET", "POST"]
    assert [item.proposal.payload["tag"] for item in stored] == [TAG_RICOVERATO]
    assert session.calls[2]["json"][0]["stato"] == "pending"


def test_senza_proposte_da_creare_non_si_scrive_niente():
    session = FakeSession(
        responses=[
            FakeResponse(payload=[{"contact_id": "cb-1", "tag": TAG_RICOVERATO}]),
            FakeResponse(payload=[]),
        ]
    )
    # Il contatto porta già il tag: la regola non ha niente da proporre.
    result = TriageResult(conversations=(_entry(),))

    stored = build_and_store_proposals(
        _config(), result, [_convo(tags=(TAG_RICOVERATO,))], store=_store(session), now=NOW
    )

    assert stored == []
    assert [call["method"] for call in session.calls] == ["GET", "GET"]


def test_un_triage_vuoto_non_interroga_il_database():
    session = FakeSession()

    stored = build_and_store_proposals(
        _config(), TriageResult(conversations=()), [], store=_store(session), now=NOW
    )

    assert stored == []
    assert session.calls == []


def test_gli_id_delle_proposte_sono_uuid_veri_di_default():
    session = FakeSession()

    [item] = _store(session).insert_pending([_proposal()])

    uuid.UUID(item.id)  # solleva se non lo è
