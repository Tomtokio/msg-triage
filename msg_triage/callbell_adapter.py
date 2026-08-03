"""Callbell implementation of the neutral :class:`SourceAdapter` interface.

Facts verified against real Callbell data (2026-07-16, see docs/dev_notes.md):
- Envelopes carry ``meta: {page, pages}``; iterate while ``page < pages``.
- Inbound/outbound is the message ``status`` field: ``received`` = client,
  ``sent`` = operator, ``note`` = internal note.
- A human note has a ``uuid`` and ``from != to``; a system note (e.g.
  "Conversation was assigned to X") has no ``uuid`` and ``from == to``.
- ``/contacts`` is ~332 pages, ordered by recent activity, and the only
  per-contact timestamp is ``createdAt`` (creation, NOT last activity), with no
  server-side sort. So the time window is the primary filter: we page contacts,
  peek each one's most recent message, and stop after ``patience`` consecutive
  out-of-window contacts. We never page all 332 pages.

Write-path facts verified on real data (2026-08-01):
- ``PATCH /contacts/:uuid`` with ``{"tags": [...]}`` REPLACES the whole tag list
  (it is an absolute set, not a delta), and an empty list really is saved.
- ``GET /contacts/:uuid`` returns ``{"contact": {...}}`` — an OBJECT. The docs
  claim an array of one element; they are wrong.

⚠️ This module can WRITE. :class:`CallbellClient` refuses every write unless it
was built with ``allow_writes=True`` — see the "Write path" section of the class.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta, timezone

import requests

from .config import Config
from .source_adapter import Conversation, Message, Role, SourceAdapter

logger = logging.getLogger(__name__)

BASE_URL = "https://api.callbell.eu/v1"
DEFAULT_THROTTLE = 0.3  # seconds between successful requests (rate-limit hygiene)
DEFAULT_PATIENCE = 30  # consecutive out-of-window contacts before we stop paging
DEFAULT_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds; fallback when a 429 carries no Retry-After


class CallbellError(RuntimeError):
    """Raised when the Callbell API returns an unrecoverable error."""


# --- Pure mapping helpers (no network; unit-tested directly with dicts) --------


def _parse_ts(value: str) -> datetime:
    """Parse a Callbell ISO-8601 timestamp into a timezone-aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_system_note(message: dict) -> bool:
    """True for provider-generated notes (no ``uuid`` and ``from == to``).

    Distinguishes automation (e.g. "Conversation was assigned to X") from a note
    a colleague actually wrote by hand.
    """
    return (
        message.get("status") == "note"
        and not message.get("uuid")
        and message.get("from") == message.get("to")
    )


def _message_role(message: dict) -> Role:
    """Map a raw Callbell message to its neutral :class:`Role`."""
    status = message.get("status")
    if status == "received":
        return Role.CLIENTE
    if status == "sent":
        return Role.OPERATORE
    if status == "note":
        return Role.NOTA_SISTEMA if _is_system_note(message) else Role.NOTA_INTERNA
    # Unknown status: bucket as system (never misattribute to a client/operator).
    logger.warning("Unknown Callbell message status %r; treating as NOTA_SISTEMA", status)
    return Role.NOTA_SISTEMA


def _to_message(message: dict) -> Message:
    """Convert one raw Callbell message into a neutral :class:`Message`."""
    return Message(
        role=_message_role(message),
        text=message.get("text") or "",
        timestamp=_parse_ts(message["createdAt"]),
    )


def _build_conversation(contact: dict, messages: tuple[Message, ...]) -> Conversation:
    """Assemble a neutral :class:`Conversation` from a raw contact + its messages."""
    channel = contact.get("channel") or {}
    return Conversation(
        contact_id=contact["uuid"],
        name=contact.get("name") or "",
        channel=channel.get("type") or "",
        tags=tuple(contact.get("tags") or ()),
        assigned_user=contact.get("assignedUser") or None,
        messages=messages,
    )


def _unwrap_contact(payload: dict, path: str) -> dict:
    """Unwrap the ``{"contact": {...}}`` envelope of the single-contact endpoints.

    VERIFIED on real data (2026-08-01): ``contact`` is an OBJECT. The Callbell docs
    describe an array of one element and are wrong — ``payload["contact"][0]`` raises
    ``KeyError``. We do not accept both shapes: if the envelope ever changes we want a
    loud, named failure here, not a guess deep inside a write loop.
    """
    contact = payload.get("contact")
    if not isinstance(contact, dict):
        raise CallbellError(
            f"unexpected envelope on {path}: expected {{'contact': {{...}}}}, "
            f"got {type(contact).__name__}"
        )
    return contact


def _retry_after_seconds(response: requests.Response, attempt: int) -> float:
    """How long to wait after a 429: honour ``Retry-After`` else exponential backoff."""
    raw = response.headers.get("Retry-After")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    return _BACKOFF_BASE * (2**attempt)


# --- HTTP client ---------------------------------------------------------------


class CallbellClient:
    """Thin, injectable HTTP client for the Callbell REST API.

    Handles auth, ``meta.page``/``meta.pages`` pagination and 429 rate limits.
    ``session`` and ``sleep`` are injected so the adapter can be unit-tested with
    no real network and no real waiting. ``request_count`` tracks successful data
    pages fetched, so callers can log the cost of a run.

    ⚠️ Read-only by default. ``allow_writes`` is the structural guard on the write
    path: with it off (the default, and what :func:`build_adapter` wires for the
    Telegram bot) the client *cannot* write, rather than merely being expected not
    to. Only the one-shot maintenance scripts opt in.
    """

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        base_url: str = BASE_URL,
        sleep=time.sleep,
        throttle: float = DEFAULT_THROTTLE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        allow_writes: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session if session is not None else requests.Session()
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._sleep = sleep
        self._throttle = throttle
        self._max_retries = max_retries
        self._allow_writes = allow_writes
        self.request_count = 0

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict:
        """One HTTP call with throttle, 429 retry and error mapping.

        The 429 retry re-sends the SAME body. That is safe for our writes because
        ``tags`` is an absolute set and not a delta, so re-sending is idempotent.
        """
        url = f"{self._base_url}{path}"
        for attempt in range(self._max_retries + 1):
            response = self._session.request(
                method, url, headers=self._headers, params=params, json=json
            )
            if response.status_code == 429:
                wait = _retry_after_seconds(response, attempt)
                logger.warning(
                    "Callbell 429 on %s %s; waiting %.1fs (retry %d)",
                    method,
                    path,
                    wait,
                    attempt + 1,
                )
                self._sleep(wait)
                continue
            if response.status_code >= 400:
                raise CallbellError(
                    f"Callbell error {response.status_code} on {method} {path}"
                )
            self.request_count += 1
            if self._throttle:
                self._sleep(self._throttle)
            return response.json()
        raise CallbellError(
            f"Callbell still rate-limited on {method} {path} "
            f"after {self._max_retries} retries"
        )

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def _paginate(
        self, path: str, list_key: str, params: dict | None = None
    ) -> Iterator[dict]:
        """Yield items across pages, following ``meta.page``/``meta.pages``.

        ``params`` carries any extra query filter (e.g. ``tags[]``) alongside ``page``.
        """
        page = 1
        while True:
            payload = self._get(path, params={**(params or {}), "page": page})
            yield from payload.get(list_key) or ()
            meta = payload.get("meta") or {}
            pages = meta.get("pages")
            current = meta.get("page", page)
            if not pages or current >= pages:
                return
            page += 1

    def iter_contacts(self) -> Iterator[dict]:
        """Yield raw contacts, ordered by recent activity, page by page."""
        return self._paginate("/contacts", "contacts")

    def iter_messages(self, contact_uuid: str) -> Iterator[dict]:
        """Yield raw messages for a contact, newest first (``createdAt`` DESC)."""
        return self._paginate(f"/contacts/{contact_uuid}/messages", "messages")

    def iter_contacts_by_tag(self, tag: str) -> Iterator[dict]:
        """Yield raw contacts carrying ``tag``, newest activity first.

        Callbell matches tags case-INSENSITIVELY, so the result can include
        variants of ``tag``. This is a way to *find* candidates cheaply, never a
        proof of what a contact carries: callers needing an exact tag must
        re-check the returned ``tags`` client-side.
        """
        return self._paginate("/contacts", "contacts", params={"tags[]": tag})

    # --- Write path ------------------------------------------------------------
    # Everything below mutates real customer records. It is unreachable unless the
    # client was built with allow_writes=True, which build_adapter() never does.

    def get_contact(self, contact_uuid: str) -> dict:
        """Fetch one raw contact — used to re-read tags immediately before a write."""
        path = f"/contacts/{contact_uuid}"
        return _unwrap_contact(self._request("GET", path), path)

    def update_contact_tags(self, contact_uuid: str, tags: Sequence[str]) -> dict:
        """REPLACE a contact's entire tag list; return the contact as Callbell saved it.

        ``tags`` is an absolute set, not a delta (verified 2026-08-01): to remove one
        tag you send every other tag, and an empty list really does clear them all.
        Deliberately the only write in this client — no generic ``patch()``, no delete,
        no sending messages, no assignments.
        """
        if not self._allow_writes:
            raise CallbellError(
                "refusing to write: this CallbellClient is read-only "
                "(built without allow_writes=True)"
            )
        path = f"/contacts/{contact_uuid}"
        payload = self._request("PATCH", path, json={"tags": list(tags)})
        return _unwrap_contact(payload, path)


# --- Neutral adapter -----------------------------------------------------------


class CallbellSourceAdapter(SourceAdapter):
    """Callbell implementation of the neutral :class:`SourceAdapter`.

    Applies the verified "case-B" fetch strategy: contacts come ordered by recent
    activity but expose no reliable last-activity timestamp, so we peek each
    contact's most recent message and stop once ``patience`` contacts in a row
    fall outside the window. Cost per run is ~``patience`` + N ``/messages`` calls
    (N = in-window contacts); it is logged at DEBUG so a degeneration is visible.
    """

    def __init__(
        self,
        client: CallbellClient,
        *,
        patience: int = DEFAULT_PATIENCE,
        now=None,
    ) -> None:
        self._client = client
        self._patience = patience
        self._now = now if now is not None else (lambda: datetime.now(timezone.utc))

    def fetch_recent_conversations(self, window_hours: float = 6.0) -> list[Conversation]:
        cutoff = self._now() - timedelta(hours=window_hours)
        requests_at_start = self._client.request_count
        conversations: list[Conversation] = []
        contacts_scanned = 0
        consecutive_out = 0

        for contact in self._client.iter_contacts():
            contacts_scanned += 1
            window_messages = self._window_messages(contact["uuid"], cutoff)
            if window_messages:
                conversations.append(_build_conversation(contact, window_messages))
                consecutive_out = 0
            else:
                consecutive_out += 1
                if consecutive_out >= self._patience:
                    break

        logger.debug(
            "windowed fetch: scanned %d contacts, %d API calls, %d conversations "
            "in window (patience=%d, window=%.1fh)",
            contacts_scanned,
            self._client.request_count - requests_at_start,
            len(conversations),
            self._patience,
            window_hours,
        )
        return conversations

    def _window_messages(
        self, contact_uuid: str, cutoff: datetime
    ) -> tuple[Message, ...]:
        """Messages within the window for one contact, in chronological order.

        Messages arrive newest-first, so we stop at the first one older than the
        cutoff — for an out-of-window contact that means peeking a single page.
        """
        collected: list[Message] = []
        for raw in self._client.iter_messages(contact_uuid):
            if _parse_ts(raw["createdAt"]) < cutoff:
                break
            collected.append(_to_message(raw))
        collected.reverse()
        return tuple(collected)


def build_adapter(
    config: Config,
    *,
    session: requests.Session | None = None,
    patience: int = DEFAULT_PATIENCE,
) -> CallbellSourceAdapter:
    """Wire a :class:`CallbellSourceAdapter` from validated :class:`Config`.

    The API key comes from ``config.callbell_api_key`` (never read from the
    environment directly here).

    The client is built WITHOUT ``allow_writes``: everything wired through here —
    the Telegram bot included — is structurally incapable of writing to Callbell.
    """
    client = CallbellClient(config.callbell_api_key, session=session)
    return CallbellSourceAdapter(client, patience=patience)
