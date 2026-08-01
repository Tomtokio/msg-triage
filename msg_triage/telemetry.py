"""Telemetry: the single point where ``vet_agents_telemetry`` is imported.

No other module imports the library directly. Everything goes through here, so
the agent keeps one place to look at when the contract changes and one place
that guarantees the two non-negotiable properties:

- **import protetto**: if the library is not installed (it is pinned to a tag and
  deliberately kept out of ``pyproject.toml``), every function here is a no-op and
  the bot boots and runs exactly as before;
- **fail-silent**: no call in this module can raise. A telemetry problem must never
  cost a triage. Failures become a local WARNING and nothing else.

``tenant_id`` is always ``"self"``: msg-triage is Tommaso's personal tool, not a
service delivered to a client, so there is no tenant to attribute anything to --
business events included.

See ``docs/telemetry_api_contract.md`` for the library contract and
``CLAUDE.md`` for this agent's event/operation vocabulary.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

AGENT_ID = "msg-triage"
TENANT_ID = "self"

try:  # the library is installed separately, pinned to a tag; absence is legal
    from vet_agents_telemetry import heartbeat as _heartbeat
    from vet_agents_telemetry import init as _init
    from vet_agents_telemetry import log_event as _log_event
    from vet_agents_telemetry import log_usage as _log_usage
except ImportError:  # pragma: no cover - exercised by the "not installed" path
    _heartbeat = _init = _log_event = _log_usage = None

# True only after a successful setup(): the library is here and init() went through.
# Every function short-circuits on it, so an absent library or a failed init collapse
# to "do nothing". TELEMETRY_ENABLED=false is NOT this flag: the library stays
# initialised and turns its own writes into no-ops, which is its job, not ours.
_active = False

# Set when crashed() fires, so shutdown() does not also emit agent_stopped: a crash
# must produce ONE closing event, not a crash followed by a clean stop. The event
# sequence has to be comparable across agents in the dashboard.
_crashed = False


def setup() -> None:
    """Initialise telemetry for this process. Call once at startup.

    MUST run after the ``.env`` is loaded: the library reads ``TELEMETRY_*`` from
    the environment and gets nothing if it runs first. ``init`` emits
    ``agent_started``, sends the first heartbeat and starts the daemon thread that
    heartbeats every 5 minutes (this is a long-running process, so that heartbeat
    is the signal that says "alive").
    """
    global _active
    if _active:
        return
    if _init is None:
        logger.info("vet_agents_telemetry non installata: telemetria non attiva")
        return
    try:
        _init(agent_id=AGENT_ID)
        _active = True
    except Exception:  # noqa: BLE001 - fail-silent: telemetry must never stop the boot
        logger.warning("Telemetry setup fallito; si prosegue senza", exc_info=True)


def event(
    type: str,
    *,
    severity: str = "info",
    message: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Log one event. Synchronous: only call from outside the event loop.

    ``metadata`` carries reference ids, counts and durations only -- never client
    names, phone numbers, message text or digest text.
    """
    if not _active:
        return
    try:
        _log_event(
            type,
            tenant_id=TENANT_ID,
            severity=severity,
            message=message,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001 - fail-silent
        logger.warning("Telemetry event %r fallito", type, exc_info=True)


async def aevent(
    type: str,
    *,
    severity: str = "info",
    message: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Async-safe :func:`event`: the write is blocking HTTP, so it goes to a thread.

    Same reason ``save_triage_run`` is called via ``asyncio.to_thread`` in the bot:
    the library talks to Supabase with a 3 s timeout, which would otherwise block
    the whole event loop. The ``_active`` guard runs first so an absent library does
    not even pay for the thread hop.
    """
    if not _active:
        return
    await asyncio.to_thread(
        event, type, severity=severity, message=message, metadata=metadata
    )


def usage(
    *,
    model: str,
    operation: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> None:
    """Record one Anthropic API call (tokens in, tokens out).

    ``cost_usd`` is deliberately not passed: the library's pricing table prices the
    model and is the single source of truth for cost. Skips silently when both
    counts are missing -- the contract requires at least one, and injected fake
    clients in the tests have no ``usage`` on their response.
    """
    if not _active:
        return
    if input_tokens is None and output_tokens is None:
        return
    try:
        _log_usage(
            provider="anthropic",
            model=model,
            operation=operation,
            tenant_id=TENANT_ID,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except Exception:  # noqa: BLE001 - fail-silent
        logger.warning("Telemetry usage per %r fallito", operation, exc_info=True)


def crashed(exc: BaseException) -> None:
    """Report a handled crash just before the process dies.

    Also sends an ``offline`` heartbeat: the dashboard dot must go red immediately,
    without waiting for the missing-heartbeat timeout. Marks the process as crashed
    so a later :func:`shutdown` does not add a contradictory ``agent_stopped``.
    """
    global _crashed
    _crashed = True
    if not _active:
        return
    try:
        _log_event(
            "agent_crashed",
            tenant_id=TENANT_ID,
            severity="error",
            metadata={"exception": type(exc).__name__},
        )
        _heartbeat("offline")
    except Exception:  # noqa: BLE001 - fail-silent, and we are dying anyway
        logger.warning("Telemetry crashed() fallito", exc_info=True)


def shutdown(reason: str = "process_exit") -> None:
    """Report a voluntary, clean stop (e.g. ``systemctl restart``).

    No-op after :func:`crashed`: one closing event per process life, so the
    dashboard can tell "restarted by Tommaso" from "died on its own".
    """
    if _crashed or not _active:
        return
    try:
        _log_event("agent_stopped", tenant_id=TENANT_ID, metadata={"reason": reason})
        _heartbeat("offline")
    except Exception:  # noqa: BLE001 - fail-silent
        logger.warning("Telemetry shutdown() fallito", exc_info=True)
