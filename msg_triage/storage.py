"""T7 — Persistence of a triage run on Supabase (PostgREST), plus the raw client.

It saves what a run produced so that three things become possible: re-reading past
triages (storico), comparing a conversation with its previous state (memory, T4 — it
queries ``conversation_states`` by ``contact_id``), and the tag/proposal lifecycle
(T10, which builds :class:`~msg_triage.proposal_store.ProposalStore` on top of the
:class:`SupabaseStore` below).

T7 itself only ever wrote. Since T10/PR2 the client also READS — the proposal rules
need to know what was already proposed and which tags are ours — which is why
``Accept-Profile`` now travels beside ``Content-Profile`` (see :class:`SupabaseStore`).

Two design rules govern the T7 entry point, :func:`save_triage_run`:

- **Never in the critical path.** Persistence is best-effort: an unreachable or
  refusing Supabase produces a WARNING and nothing else, and the triage is delivered
  on Telegram exactly as before. :func:`save_triage_run` is the single place that
  guarantees "never raises", so callers need no try/except of their own.
- **Off by default until real credentials exist.** Production runs today with the
  placeholder ``SUPABASE_URL=unused``, put there only to satisfy the boot-time
  fail-fast. Anything that is not an ``https://`` URL means "no persistence", so the
  bot behaves identically to today with no extra env var to remember.

HTTP: plain ``requests`` against PostgREST rather than ``supabase-py``. Two POSTs do
not justify the ``gotrue``/``realtime``/``storage3`` tree, ``requests`` is already a
dependency, and :class:`~msg_triage.callbell_adapter.CallbellClient` already
establishes the shape — a thin client with an injected ``session``, unit-tested with
no network. The custom schema is addressed with the ``Content-Profile`` header (see
:class:`SupabaseStore`).

Privacy: these rows carry real client names and clinical information. Error logs
report the HTTP status and PostgREST's ``code``/``message`` only — never ``details``
or ``hint``, which can echo the offending row back into the journal.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

import requests

from .config import Config
from .renderers import RenderedTriage
from .source_adapter import Conversation
from .triage_engine import ConversationTriage, TriageResult, extract_species

logger = logging.getLogger(__name__)

# The custom schema created by migrations/0001_msg_triage_schema.sql, inside the
# shared "agents-telemetry" project. PostgREST reaches it via Content-Profile.
SCHEMA = "msg_triage"

RUNS_TABLE = "triage_runs"
STATES_TABLE = "conversation_states"

# Generous enough for a cold PostgREST, short enough that a hanging Supabase cannot
# hold the bot: the save runs after delivery, so this is a bounded tail, not a delay.
DEFAULT_TIMEOUT = 10.0


# --- Configuration gate --------------------------------------------------------


def is_configured(config: Config) -> bool:
    """True when ``SUPABASE_URL`` looks like a real project URL.

    The structural check IS the feature flag. Production carries the placeholder
    ``unused`` (required by the boot-time validation, see ``docs/runbook.md``), and
    without an ``https://`` URL there is simply nothing to call. The upside over an
    explicit ``SUPABASE_ENABLED`` is that persistence starts the moment real
    credentials land in ``.env`` — nothing else to switch on and later wonder about.

    The value is already stripped and non-blank: ``load_config`` guarantees it.
    """
    return config.supabase_url.startswith("https://")


# --- Record building (pure: no network, no clock, no ids of its own) -----------


def build_run_record(
    rendered: RenderedTriage,
    *,
    run_id: str,
    window_hours: float,
    n_conversations: int,
) -> dict:
    """The ``triage_runs`` row for one run.

    ``created_at`` is left to the database default. ``n_conversations`` is the number
    of child rows, not of conversations fetched — the triage may omit one — so the
    row stays consistent with its children.
    """
    return {
        "id": run_id,
        "window_hours": window_hours,
        "n_conversations": n_conversations,
        "schema_text": rendered.schema_text,
        "table_text": rendered.table_text,
        "vocal_text": rendered.vocal_text,
    }


def _last_message_at(convo: Conversation | None) -> str | None:
    """ISO-8601 timestamp of a conversation's most recent message, or ``None``.

    ``messages`` is chronological, so the last one is the most recent. An empty
    conversation does not occur with the Callbell adapter (it only builds one when the
    window yielded messages), but the column is nullable so a source that behaves
    differently loses one field instead of the whole insert.
    """
    if convo is None or not convo.messages:
        return None
    timestamp: datetime = convo.messages[-1].timestamp
    return timestamp.isoformat()


def _build_state_record(
    entry: ConversationTriage, convo: Conversation | None, *, run_id: str
) -> dict:
    """One ``conversation_states`` row: the whole triage entry, plus the two fields
    the entry does not carry — ``specie`` (extracted from the model's text) and
    ``last_message_at`` (which lives on the neutral conversation).

    Columns mirror the dataclass attribute names one-to-one, so there is no mapping
    to keep in sync, and nothing the triage JUDGED about a conversation is dropped —
    which is what keeps T4 from needing a second migration.

    One deliberate exception since T10: ``fatti`` is NOT persisted. There are no
    columns for it, and migrations are applied by hand; the facts are consumed
    inside the run that produced them, by the proposal rules. Note also that
    ``specie`` keeps coming from the ``**...**`` marker in the model's prose and not
    from ``fatti.animali``: otherwise the very same messages would store a different
    ``specie`` depending on a feature flag, and the history would become
    flag-dependent.
    """
    promessa = entry.promessa_rilevata
    return {
        "run_id": run_id,
        "contact_id": entry.contact_id,
        "nome": entry.nome,
        "gruppo": entry.gruppo.value,
        "motivo": entry.motivo,
        "urgenza": entry.urgenza.value,
        "presidio": entry.presidio.value,
        "temperatura": entry.temperatura.value,
        "stato_sintetico": entry.stato_sintetico,
        "azione_suggerita": entry.azione_suggerita,
        "specie": extract_species(entry),
        "last_message_at": _last_message_at(convo),
        "promessa_testo": promessa.testo if promessa else None,
        "promessa_scadenza_stimata": promessa.scadenza_stimata if promessa else None,
    }


def build_state_records(
    result: TriageResult, conversations: list[Conversation], *, run_id: str
) -> list[dict]:
    """The ``conversation_states`` rows for one run, one per triaged conversation.

    ``conversations`` (the neutral format) is indexed by ``contact_id`` to recover
    ``last_message_at``; a triage entry always comes from one of them, since its
    ``contact_id`` is filled from the source and never by the model.
    """
    by_contact = {convo.contact_id: convo for convo in conversations}
    return [
        _build_state_record(entry, by_contact.get(entry.contact_id), run_id=run_id)
        for entry in result.conversations
    ]


# --- PostgREST client ----------------------------------------------------------


class SupabaseError(RuntimeError):
    """Raised inside the store when an insert fails; never escapes this module."""


class SupabaseStore:
    """Thin, injectable PostgREST client for the custom schema.

    ``session`` is injected so the store is unit-testable with no real network, like
    :class:`~msg_triage.callbell_adapter.CallbellClient`. The headers that matter for a
    CUSTOM schema:

    - ``Content-Profile: msg_triage`` — routes WRITES to the schema. Without it
      PostgREST looks in ``public`` and reports a missing table.
    - ``Accept-Profile: msg_triage`` — the same thing for READS, and a separate header.
      A GET carrying only ``Content-Profile`` goes looking in ``public`` and fails in a
      way that reads like a missing grant. Nothing read before T10/PR2, so nothing had
      noticed.
    - ``Prefer`` is per-call, not a constant on the client: it governs what a write
      returns, and on a GET it is at best noise.

    All of it also requires ``msg_triage`` to be listed under the project's "Exposed
    schemas" and the key to be the service_role JWT in legacy ``eyJh...`` form; see
    ``docs/runbook.md`` § E.
    """

    def __init__(
        self,
        url: str,
        key: str,
        *,
        schema: str = SCHEMA,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = url.rstrip("/")  # a pasted URL often carries a trailing slash
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Content-Profile": schema,
            "Accept-Profile": schema,
        }

    def _send(self, table: str, verb, **kwargs) -> requests.Response:
        """One PostgREST round-trip: run it, map transport and HTTP failures.

        ``verb`` is the session method itself (``self._session.post`` & co.) rather
        than a string, so each caller still reads as the HTTP verb it is while the
        error mapping lives in one place.
        """
        url = f"{self._base_url}/rest/v1/{table}"
        try:
            response = verb(url, timeout=self._timeout, **kwargs)
        except requests.RequestException as exc:
            raise SupabaseError(f"{table}: {type(exc).__name__}: {exc}") from exc
        if not 200 <= response.status_code < 300:
            raise SupabaseError(f"{table}: HTTP {response.status_code} — {_error_detail(response)}")
        return response

    def insert(self, table: str, rows: list[dict], *, on_conflict: str | None = None) -> None:
        """POST ``rows`` into ``table``. Raises :class:`SupabaseError` on failure.

        ``return=minimal``: we generate every id ourselves, so there is nothing to read
        back and no row echoed into a response.

        ``on_conflict`` names the unique constraint's columns and turns the insert into
        an UPSERT. Used for ``system_tags``, whose ``unique (contact_id, tag)`` would
        otherwise turn a re-application into a 409 — and a 409 there would report as
        "failed" a proposal whose write on Callbell had already gone through.
        """
        prefer = "return=minimal"
        params = None
        if on_conflict is not None:
            prefer = f"{prefer},resolution=merge-duplicates"
            params = {"on_conflict": on_conflict}
        self._send(
            table,
            self._session.post,
            headers={**self._headers, "Prefer": prefer},
            params=params,
            json=rows,
        )

    @staticmethod
    def _rows(table: str, response: requests.Response) -> list[dict]:
        """The JSON list PostgREST promises, or a :class:`SupabaseError`.

        Both failure modes are the same mistake to a caller: a proxy error page and a
        successful-looking non-list body must not pass for "no rows". Callers of this
        class catch one exception type, so a bare ``ValueError`` from ``.json()`` would
        escape straight past them.
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise SupabaseError(f"{table}: response body was not JSON") from exc
        if not isinstance(body, list):
            raise SupabaseError(f"{table}: expected a JSON list, got {type(body).__name__}")
        return body

    def select(self, table: str, params: dict) -> list[dict]:
        """GET the rows matching ``params`` (PostgREST filter syntax)."""
        return self._rows(
            table, self._send(table, self._session.get, headers=self._headers, params=params)
        )

    def patch(
        self, table: str, params: dict, row: dict, *, prefer: str = "return=minimal"
    ) -> list[dict]:
        """PATCH the rows matched by ``params``; return them only if ``prefer`` asks.

        The filter is mandatory and refused when empty: PostgREST cheerfully updates
        the WHOLE table when a PATCH carries none, and there is no undo for that.
        """
        if not params:
            raise SupabaseError(f"{table}: refusing an unfiltered PATCH")
        response = self._send(
            table,
            self._session.patch,
            headers={**self._headers, "Prefer": prefer},
            params=params,
            json=row,
        )
        # With return=minimal PostgREST answers 204 with no body at all: asking for JSON
        # there would raise on the happy path.
        return self._rows(table, response) if "representation" in prefer else []

    def delete(self, table: str, params: dict) -> None:
        """DELETE the rows matched by ``params`` (PostgREST filter syntax).

        The filter is mandatory and refused when empty, for the same reason as
        :meth:`patch`: an unfiltered DELETE empties the whole table and there is no undo.
        """
        if not params:
            raise SupabaseError(f"{table}: refusing an unfiltered DELETE")
        self._send(
            table,
            self._session.delete,
            headers={**self._headers, "Prefer": "return=minimal"},
            params=params,
        )

    def save_run(self, run: dict, states: list[dict]) -> None:
        """Insert the run, then all its states in one bulk insert.

        Order is mandatory: ``conversation_states.run_id`` is a foreign key. If the
        second call fails the run row stays behind without children — diagnosable, and
        harmless for T4, which reads the latest state per contact and simply finds
        nothing from this run.
        """
        self.insert(RUNS_TABLE, [run])
        if states:
            self.insert(STATES_TABLE, states)


def _error_detail(response: requests.Response) -> str:
    """A log-safe summary of a PostgREST error body.

    Only ``code`` and ``message`` are reported. ``details`` and ``hint`` are dropped
    on purpose: PostgREST echoes the offending row into them, which for us means
    client names and clinical prose in the journal (dev_notes, "niente log persistente
    del contenuto in chiaro oltre il necessario"). A non-JSON body (a proxy error
    page) yields nothing but the status the caller already has.
    """
    try:
        body = response.json()
    except ValueError:
        return "non-JSON body"
    if not isinstance(body, dict):
        return "unexpected body"
    code = body.get("code") or "?"
    message = body.get("message") or "?"
    return f"{code}: {message}"


def build_store(config: Config, *, session: requests.Session | None = None) -> SupabaseStore | None:
    """A store wired from config, or ``None`` when Supabase is not configured."""
    if not is_configured(config):
        return None
    return SupabaseStore(config.supabase_url, config.supabase_key, session=session)


# --- Entry point ---------------------------------------------------------------


def save_triage_run(
    config: Config,
    result: TriageResult,
    rendered: RenderedTriage,
    conversations: list[Conversation],
    *,
    window_hours: float,
    store: SupabaseStore | None = None,
    new_id=uuid.uuid4,
) -> bool:
    """Save one run. Best-effort: NEVER raises. Returns True if it wrote.

    With placeholder credentials it returns False without touching the network. Any
    failure — network, HTTP, a schema drift nobody noticed — becomes a WARNING: the
    triage has already been delivered on Telegram and must not be undone by a storage
    problem. ``store`` and ``new_id`` are injected in tests (same style as ``now`` in
    the engine and the adapter).
    """
    try:
        # Everything, store construction included, sits inside the guarantee: a
        # failure while BUILDING the client would break the promise just as badly
        # as a failure while using it.
        if store is None:
            store = build_store(config)
        if store is None:
            logger.debug("Supabase not configured: run not saved (expected until credentials land)")
            return False

        run_id = str(new_id())
        run = build_run_record(
            rendered,
            run_id=run_id,
            window_hours=window_hours,
            n_conversations=len(result.conversations),
        )
        states = build_state_records(result, conversations, run_id=run_id)
        store.save_run(run, states)
        logger.info("Saved triage run %s (%d conversation states)", run_id, len(states))
        return True
    except Exception as exc:  # noqa: BLE001 - the "never raises" guarantee lives here
        logger.warning("Supabase save failed (%s); triage delivered anyway", exc)
        return False
