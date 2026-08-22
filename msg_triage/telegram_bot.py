"""T8 — Telegram bot: command interface and delivery for the triage.

Read-only toward clients: it never replies to WhatsApp, it only delivers the
triage to the authorized operator on Telegram. It orchestrates the working
pipeline T2 (fetch) -> T3 (triage) -> T5 (render) and sends the three formats as
three distinct messages (dev_notes: never one block), then saves the run (T7).

The save comes AFTER delivery, in its own thread hop: persistence is best-effort
and must never sit in the critical path, not even as latency. It cannot raise (see
:func:`~msg_triage.storage.save_triage_run`), so it needs no guard here.

Not wired yet (seams in place, no rework when they land):
- T4 memory: ``triage`` is called without ``previous_state``; ``_memory_clause``
  in the renderers still returns ``""``. The state it will read is now being
  written by T7.
- T6 audio (TTS): the "vocale" is delivered as text; the single swap point is
  marked ``SEAM T6`` in :func:`_deliver_triage`.

T10 (``ENABLE_PROPOSALS``, default off): after the three formats a run delivers every
ripe pending proposal, one message each with ✅/❌. A tap claims the row in the database
— that is the double-tap defence — and only ✅ reaches Callbell, through
:mod:`msg_triage.proposal_executor`. The fetch path is still structurally read-only:
``build_adapter`` grants no ``allow_writes``, and ``build_write_client`` is built here
only inside the confirmed branch of the callback.

Design: python-telegram-bot v21+ is async, but the pipeline (requests + anthropic)
is blocking, so the heavy work runs off the event loop via ``asyncio.to_thread``.
The heavy logic lives in pure sync functions (unit-testable in the house style with
injected fakes); the async handlers are thin glue. ``telegram`` is imported lazily
inside the factory/launcher/error-handler, so importing this module (e.g. to test
the pure helpers) needs no telegram install and stays light.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from msg_triage import telemetry
from msg_triage.callbell_adapter import (
    CallbellError,
    build_adapter,
    build_read_client,
    build_write_client,
)
from msg_triage.config import Config
from msg_triage.proposal_executor import (
    ExecutionOutcome,
    execute,
    load_deliverable_with_names,
)
from msg_triage.proposal_store import (
    ProposalStore,
    build_and_store_proposals,
    build_proposal_store,
    row_to_stored,
)
from msg_triage.proposals import StatoProposta
from msg_triage.renderers import RenderedTriage, render_all, render_proposal
from msg_triage.source_adapter import Conversation
from msg_triage.storage import SupabaseError, save_triage_run
from msg_triage.triage_engine import TriageError, TriageResult, build_triage_engine

if TYPE_CHECKING:  # annotations only — no runtime telegram dependency here
    from telegram import Update
    from telegram.ext import Application, ContextTypes

logger = logging.getLogger(__name__)

# Telegram rejects any single message longer than this many characters.
TELEGRAM_MESSAGE_LIMIT = 4096

# /triage window bounds. Default mirrors the adapter's default window.
DEFAULT_WINDOW_HOURS = 6.0
_MAX_WINDOW_HOURS = 168.0  # one week: a sane upper bound for the argument

# T10 proposal buttons. Telegram caps callback_data at 64 bytes; "t10:ok:" plus a uuid4
# is 43, so the proposal id travels whole and nothing has to be looked up by position.
CALLBACK_PREFIX = "t10"
CALLBACK_OK = "ok"
CALLBACK_NO = "no"


# --- Pure helpers (sync, no network, no async — the testable core) -------------


def parse_window_hours(arg: str | None) -> float:
    """Parse the optional ``/triage`` window argument into a positive hour count.

    ``None`` / empty -> the default window. Raises ``ValueError`` with an Italian
    message (shown to the user) if the argument is not a finite number or falls
    outside ``(0, 168]``. Accepts the Italian decimal comma ("6,5").
    """
    if arg is None:
        return DEFAULT_WINDOW_HOURS
    text = arg.strip().replace(",", ".")
    if not text:
        return DEFAULT_WINDOW_HOURS
    try:
        hours = float(text)
    except ValueError:
        raise ValueError(
            f"«{arg}» non è un numero di ore valido. Uso: /triage oppure /triage 12."
        ) from None
    if not math.isfinite(hours) or hours <= 0 or hours > _MAX_WINDOW_HOURS:
        raise ValueError(
            f"Le ore devono essere un numero tra 0 (escluso) e {int(_MAX_WINDOW_HOURS)}."
        )
    return hours


def _safe_cut(line: str, start: int, limit: int) -> int:
    """How many chars to take from ``line[start:]`` (<= ``limit``) so a hard-split
    never lands inside an HTML tag.

    If the ``limit``-long slice would end with an unclosed ``<`` (a ``<`` with no
    ``>`` after it), back the cut up to just before that ``<``. Returns at least 1
    (falls back to ``limit`` when the ``<`` sits at offset 0) so a pathological
    >limit tag still makes progress instead of looping forever.
    """
    piece = line[start : start + limit]
    lt = piece.rfind("<")
    if lt > 0 and piece.find(">", lt) == -1:
        return lt
    return limit


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` characters.

    Prefers newline boundaries so lines stay intact; a single line longer than
    ``limit`` is hard-split as a last resort. Never returns an empty list (an empty
    string yields one empty chunk). Needed because the schema/table (full giornale
    di bordo) can exceed Telegram's per-message limit.

    HTML-safe: schema/table go out with ``parse_mode="HTML"``, and every tag pair we
    emit lives on a single physical line, so the newline path never straddles a pair.
    The last-resort hard-split uses :func:`_safe_cut` so it never cuts a tag in half.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            # Flush what we have, then hard-split the overlong line (never mid-tag).
            if current:
                chunks.append(current)
                current = ""
            start = 0
            while len(line) - start > limit:
                cut = _safe_cut(line, start, limit)
                chunks.append(line[start : start + cut])
                start += cut
            current = line[start:]  # remainder seeds the next chunk
            continue
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = line
    if current or not chunks:
        chunks.append(current)
    return chunks


def _elapsed_ms(since: float) -> int:
    """Milliseconds since a ``time.monotonic()`` mark (telemetry durations)."""
    return int((time.monotonic() - since) * 1000)


def build_callback_data(action: str, proposal_id: str) -> str:
    """The payload behind a ✅/❌ button: ``t10:ok:<id>``."""
    return f"{CALLBACK_PREFIX}:{action}:{proposal_id}"


def parse_callback_data(data: str | None) -> tuple[str, str] | None:
    """``(action, proposal_id)`` from a button payload, or ``None`` if it is not ours.

    Strict on purpose: the prefix, one of the two known actions, and a non-empty id.
    Anything else — a payload from an older build, a truncated one — gets no
    interpretation at all, because the alternative is acting on a guessed proposal id.
    """
    if not data:
        return None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    prefix, action, proposal_id = parts
    if prefix != CALLBACK_PREFIX or action not in (CALLBACK_OK, CALLBACK_NO):
        return None
    return (action, proposal_id) if proposal_id else None


def run_triage_pipeline(
    config: Config,
    hours: float,
    *,
    adapter=None,
    engine=None,
    job_id: str | None = None,
) -> tuple[TriageResult, RenderedTriage, list[Conversation]]:
    """Run the synchronous T2 -> T3 -> T5 pipeline; return result, rendering, source.

    Blocking (requests + anthropic): the async handler runs it via
    ``asyncio.to_thread``. ``adapter``/``engine`` are injectable for tests; when
    omitted they are built from ``config``. Memory (T4) is not wired — ``triage`` is
    called without ``previous_state``.

    The neutral conversations come back too because persistence needs
    ``last_message_at``, which lives on the source conversation and not on the triage
    judgment. They stay in the neutral format, so nothing Callbell-specific travels.

    ``job_id`` only labels the two telemetry events emitted at the internal stage
    boundaries: the caller sees a single thread hop and could not place them itself.
    Telemetry is synchronous here because this function already runs off the event loop.
    """
    adapter = adapter if adapter is not None else build_adapter(config)
    fetch_started = time.monotonic()
    conversations = adapter.fetch_recent_conversations(window_hours=hours)
    telemetry.event(
        "conversations_fetched",
        metadata={
            "job_id": job_id,
            "n_conversations": len(conversations),
            "duration_ms": _elapsed_ms(fetch_started),
        },
    )

    engine = engine if engine is not None else build_triage_engine(config)
    judge_started = time.monotonic()
    result = engine.triage(conversations)  # SEAM T4: previous_state intentionally omitted
    telemetry.event(
        "triage_judged",
        metadata={
            "job_id": job_id,
            "n_conversations": len(conversations),
            "n_triaged": len(result.conversations),
            "duration_ms": _elapsed_ms(judge_started),
        },
    )

    rendered = render_all(result)
    return result, rendered, conversations


def _hours_label(hours: float) -> str:
    """Human-facing Italian label for a window, e.g. "1 ora" / "12 ore" / "6.5 ore"."""
    return "1 ora" if hours == 1 else f"{hours:g} ore"


# --- Async handlers (thin glue over the pure core) -----------------------------


async def _send(message, text: str, *, parse_mode: str | None = None) -> None:
    """Send possibly-long text as one or more Telegram messages.

    ``parse_mode`` is forwarded to Telegram (``"HTML"`` for schema/table, ``None``
    for the plain voice). ``split_message`` keeps HTML tags intact across chunk
    boundaries, so every chunk is independently valid markup.
    """
    for chunk in split_message(text):
        await message.reply_text(chunk, parse_mode=parse_mode)


async def _deliver_triage(message, rendered: RenderedTriage) -> None:
    """Send the three formats as three distinct messages (each chunked).

    Schema and table are HTML (bold names, italic species, status symbols); the
    voice stays plain text so no markup is spoken or leaks into the audio path.
    """
    await _send(message, f"📋 SCHEMA\n\n{rendered.schema_text}", parse_mode="HTML")
    await _send(message, f"🧾 TABELLA\n\n{rendered.table_text}", parse_mode="HTML")
    # SEAM T6: when the TTS lands, the "vocale" becomes an audio file here instead
    # of text; nothing else in the pipeline changes.
    await _send(message, f"🔊 VOCALE\n\n{rendered.vocal_text}")


# --- T10: proposals with buttons (PR3) -----------------------------------------


def _proposal_keyboard(proposal_id: str):
    """The two-button inline keyboard of one proposal. ✅ applies, ❌ refuses."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Applica", callback_data=build_callback_data(CALLBACK_OK, proposal_id)
                ),
                InlineKeyboardButton(
                    "❌ Ignora", callback_data=build_callback_data(CALLBACK_NO, proposal_id)
                ),
            ]
        ]
    )


async def deliver_proposals(message, config: Config) -> int:
    """Send every ripe undelivered proposal, one message each. Returns how many went out.

    After the three triage formats, never mixed into them: a question that writes on a
    real record must not arrive inside a wall of prose.

    Reads from the database rather than from what this run just created, so the queue is
    the queue — the rows PR2 left pending are delivered too, oldest first, and a scheduled
    removal shows up the first time somebody runs a triage after its morning.

    Send first, record second: ``mark_delivered`` is what keeps a row from being asked
    twice, so writing it before a failed send would bury the question forever.
    """
    store = build_proposal_store(config)
    if store is None:
        return 0
    client = build_read_client(config)
    pending = await asyncio.to_thread(
        load_deliverable_with_names, store, client, now=datetime.now(timezone.utc)
    )
    for stored, nome in pending:
        sent = await message.reply_text(
            render_proposal(stored.proposal, nome=nome),
            parse_mode="HTML",
            reply_markup=_proposal_keyboard(stored.id),
        )
        await asyncio.to_thread(store.mark_delivered, stored.id, sent.message_id)
    return len(pending)


async def _edit_outcome(query, line: str) -> None:
    """Replace the proposal message with itself plus one outcome line, buttons gone.

    The question stays visible above the answer: months later the chat still says what
    was asked and what came of it. Passing no ``reply_markup`` is what removes the
    keyboard, so the same call closes the question and records it.

    Best-effort: an edit that fails (an old message, a network blip) is logged and
    nothing more. By the time we are here the decision is already in the database and any
    write on Callbell has already happened — the chat is the report, not the state.
    """
    original = getattr(query.message, "text_html", None) or getattr(query.message, "text", "")
    text = f"{original}\n\n{line}" if original else line
    try:
        await query.edit_message_text(text=text, parse_mode="HTML")
    except Exception:  # noqa: BLE001 - cosmetic; the state is already settled
        logger.warning("Modifica del messaggio della proposta fallita", exc_info=True)


async def _close_proposal(
    store: ProposalStore, proposal_id: str, *, stato: StatoProposta, executed_at=None
) -> None:
    """Write the final state of a proposal, and never let that write break the handler.

    The action on Callbell has already happened at this point. Losing the row's outcome
    is bad — it will read as ``approvata`` forever — but raising here would replace a
    wrong-looking row with a silent crash and no message at all.
    """
    try:
        await asyncio.to_thread(
            store.mark_outcome, proposal_id, stato=stato, executed_at=executed_at
        )
    except SupabaseError:
        logger.warning("Stato finale della proposta non salvato", exc_info=True)


async def _proposals_step(
    message, config: Config, result: TriageResult, conversations: list[Conversation]
) -> None:
    """Build this run's proposals, then deliver the whole ripe queue.

    Runs on EVERY completed run, empty window included: the queue does not belong to
    this run. A removal that ripened at 07:00 must arrive on a quiet morning too, and
    the rows waiting from previous runs are not this window's business either.

    Unlike ``save_triage_run`` this CAN raise (invariante 3: a proposal we cannot
    remember making must not exist), hence the guard. The triage is already delivered, so
    a problem here costs the proposals and nothing else — and it is said out loud,
    because silence would read as "there was nothing to propose".
    """
    try:
        await asyncio.to_thread(build_and_store_proposals, config, result, conversations)
        await deliver_proposals(message, config)
    except Exception as exc:  # noqa: BLE001 - the triage is delivered; this must not undo it
        logger.warning("Proposte T10 non disponibili (%s)", type(exc).__name__)
        await message.reply_text("⚠️ Proposte non disponibili in questo run.")


async def proposal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a tap on ✅/❌: claim the proposal, then execute it or file it as refused.

    ``CallbackQueryHandler`` takes no ``filters``, so the whitelist is enforced here and
    in the same spirit as everywhere else: an unauthorized tap gets NO answer at all, not
    even an empty one. Answering would confirm the bot exists.

    ``answer()`` counts once per query, which is why the flag check comes before it: an
    empty answer first would turn the "Proposte disattivate" toast into an invisible one.
    """
    config: Config = context.bot_data["config"]
    query = update.callback_query
    if query is None:
        return
    user = update.effective_user
    if user is None or user.id != config.telegram_allowed_user_id:
        logger.warning("Tap su una proposta da un utente non autorizzato: ignorato")
        return

    parsed = parse_callback_data(query.data)
    if parsed is None:
        return
    action, proposal_id = parsed

    store = build_proposal_store(config)
    if store is None:
        # The kill switch stays reversible: no edit, so the buttons survive and the row
        # stays pending. Turn the flag back on and the same tap works.
        await query.answer("Proposte disattivate")
        return
    await query.answer()

    now = datetime.now(timezone.utc)
    claimed = StatoProposta.APPROVATA if action == CALLBACK_OK else StatoProposta.RIFIUTATA
    try:
        row = await asyncio.to_thread(
            store.claim, proposal_id, stato=claimed, decided_at=now
        )
    except SupabaseError:
        logger.warning("Claim della proposta fallito", exc_info=True)
        await _edit_outcome(query, "⚠️ Database non raggiungibile: riprova.")
        return

    # The claim is the double-tap defence: it moves the row out of `pending` atomically,
    # so a second tap matches nothing and finds this branch instead of a second write.
    if row is None:
        await _edit_outcome(query, "↩️ Proposta già gestita.")
        return

    if action == CALLBACK_NO:
        await _edit_outcome(query, "❌ Ignorata.")
        return

    stored = row_to_stored(row)
    if stored is None:
        # Already claimed, and unreadable: closing it as `fallita` is the only honest end
        # — left `approvata` it would sit there forever looking like work in progress.
        await _close_proposal(store, proposal_id, stato=StatoProposta.FALLITA)
        await _edit_outcome(query, "⚠️ Proposta illeggibile: non eseguita.")
        return

    await _edit_outcome(query, "⏳ Applico…")
    try:
        outcome = await asyncio.to_thread(
            execute, stored, client=build_write_client(config), store=store, now=now
        )
    except CallbellError as exc:
        logger.warning("Esecuzione della proposta fallita: %s", exc)
        outcome = ExecutionOutcome(False, f"Errore su Callbell: {exc}")
    except SupabaseError as exc:
        # The executor swallows storage failures that happen AFTER a successful PATCH, so
        # anything reaching here happened before one: nothing was written, and saying so
        # spares a trip to Callbell to check.
        logger.warning("Esecuzione della proposta fallita: %s", exc)
        outcome = ExecutionOutcome(
            False, f"Errore sul database, niente scritto su Callbell: {exc}"
        )

    await _close_proposal(
        store,
        proposal_id,
        stato=StatoProposta.ESEGUITA if outcome.ok else StatoProposta.FALLITA,
        executed_at=now if outcome.ok else None,
    )
    symbol = "✅" if outcome.ok else "⚠️"
    await _edit_outcome(query, f"{symbol} {html.escape(outcome.message, quote=False)}")


async def _failed(
    job_id: str, started: float, exc: BaseException, *, reason: str | None = None
) -> None:
    """Close a run as failed in the telemetry.

    The exception MESSAGE never travels: only its class name and a fixed snake_case
    code. Callbell/triage errors are ours and look harmless today, but the readable
    text belongs in journald (where it already goes), not in a dashboard nobody
    filters — same stance as ``storage._error_detail``, which drops the PostgREST
    details because they echo the offending row.
    """
    if reason is None:
        if isinstance(exc, CallbellError):
            reason = "callbell_error"
        elif isinstance(exc, TriageError):
            reason = "triage_error"
        else:
            reason = "unexpected_error"
    await telemetry.aevent(
        "processing_failed",
        severity="error",
        metadata={
            "job_id": job_id,
            "reason": reason,
            "exception": type(exc).__name__,
            "duration_ms": _elapsed_ms(started),
        },
    )


async def triage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/triage [ore]``: fetch, triage, render, deliver the three formats.

    Reaches here only for the whitelisted user (the handler filter guarantees it).
    A concurrency lock rejects overlapping runs; the blocking pipeline runs off the
    event loop; Callbell/triage errors become friendly Italian replies.
    """
    config: Config = context.bot_data["config"]
    lock: asyncio.Lock = context.bot_data["triage_lock"]
    message = update.effective_message
    if message is None:
        return

    # Deterministic validation before any heavy work (deterministico prima di inferenza).
    arg = context.args[0] if context.args else None
    try:
        hours = parse_window_hours(arg)
    except ValueError as exc:
        await message.reply_text(str(exc))
        return

    if lock.locked():
        await message.reply_text("⏳ Un triage è già in corso. Attendi che finisca.")
        return

    async with lock:
        label = _hours_label(hours)
        # After the deterministic gates: every processing_started gets its closing
        # event. Telemetry-only id, unrelated to the triage_runs row (which exists
        # only for successful non-empty runs, i.e. the cases needing no debugging).
        job_id = f"msg-triage-{uuid.uuid4().hex[:8]}"
        run_started = time.monotonic()
        # Telemetry always follows the user-facing message, never precedes it: like
        # persistence, it must not sit in the critical path, not even as latency.
        await message.reply_text(
            f"🔍 Recupero le conversazioni delle ultime {label} e le analizzo…"
        )
        await telemetry.aevent(
            "processing_started", metadata={"job_id": job_id, "window_hours": hours}
        )
        try:
            result, rendered, conversations = await asyncio.to_thread(
                run_triage_pipeline, config, hours, job_id=job_id
            )
        except (CallbellError, TriageError) as exc:
            logger.warning("Triage fallito: %s", exc)
            await message.reply_text(f"⚠️ Errore durante il triage: {exc}")
            await _failed(job_id, run_started, exc)
            return
        except Exception as exc:  # noqa: BLE001 - last resort; the user must not be left hanging
            logger.exception("Errore imprevisto durante il triage")
            await message.reply_text(
                "⚠️ Errore imprevisto durante il triage. Controlla i log."
            )
            await _failed(job_id, run_started, exc)
            return

        if not result.conversations:
            # Nothing to save either: a triage_runs row implies conversations.
            await message.reply_text(
                f"✅ Nessuna conversazione con attività nelle ultime {label}."
            )
            # Still a completed run, not a failure: nothing happened, and saying so
            # is the correct outcome. Keeps every started/completed pair intact.
            await telemetry.aevent(
                "processing_completed",
                metadata={
                    "job_id": job_id,
                    "n_conversations": len(conversations),
                    "n_triaged": 0,
                    "delivered": False,
                    "duration_ms": _elapsed_ms(run_started),
                },
            )
            # Nothing to judge does not mean nothing to ask: a scheduled removal that
            # ripened this morning is waiting whatever this window contained.
            await _proposals_step(message, config, result, conversations)
            return

        try:
            await _deliver_triage(message, rendered)
        except Exception as exc:  # noqa: BLE001 - report, then fail exactly as before
            await _failed(job_id, run_started, exc, reason="delivery_failed")
            raise

        await telemetry.aevent(
            "processing_completed",
            metadata={
                "job_id": job_id,
                "n_conversations": len(conversations),
                "n_triaged": len(result.conversations),
                "delivered": True,
                "duration_ms": _elapsed_ms(run_started),
            },
        )

        # T10/PR3: after the three formats and before the best-effort save.
        await _proposals_step(message, config, result, conversations)

        # T7: best-effort, after delivery, off the event loop. Cannot raise.
        await asyncio.to_thread(
            save_triage_run, config, result, rendered, conversations, window_hours=hours
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start`` and ``/help`` (whitelisted): show the usage.

    The line about the proposals appears only when they are on: with the flag off the bot
    is read-only end to end, and promising buttons that will never arrive is worse than
    saying nothing.
    """
    config: Config = context.bot_data["config"]
    message = update.effective_message
    if message is None:
        return
    lines = [
        "Triage delle conversazioni WhatsApp della clinica (sola lettura).",
        "• /triage — ultime 6 ore",
        "• /triage 12 — ultime 12 ore",
        "Rispondo con tre messaggi: schema, tabella e vocale (sintesi).",
    ]
    if config.enable_proposals:
        lines.append(
            "Dopo i tre messaggi possono arrivare proposte di tag o rinomina, "
            "una per messaggio: scrivo su Callbell solo se tocchi ✅."
        )
    await message.reply_text("\n".join(lines))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last-resort handler for errors not caught inside a command handler."""
    logger.error("Errore non gestito nel bot", exc_info=context.error)
    from telegram import Update  # local import: keeps module top telegram-free

    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "⚠️ Errore imprevisto. Controlla i log."
            )
        except Exception:  # noqa: BLE001 - best-effort notification only
            logger.exception("Invio della notifica di errore fallito")

    # warning, not error: this handler is also where the long polling drops its own
    # failures (NetworkError, 409 Conflict, RetryAfter — with update=None), and PTB
    # retries out of them by itself. An `error` would light the dashboard red for 24h
    # over a fault that already healed. A triage that really failed asks for attention
    # through its own processing_failed, which stays `error`.
    await telemetry.aevent(
        "bot_error",
        severity="warning",
        metadata={"exception": type(context.error).__name__},
    )


# --- Wiring (telegram imported lazily) -----------------------------------------


def build_bot(config: Config) -> Application:
    """Build the Telegram ``Application`` wired from validated :class:`Config`.

    Whitelist: only ``config.telegram_allowed_user_id`` can invoke the commands.
    The filter is the whitelist — any other user's update matches no handler and
    gets NO reply (silent: no message, no typing, no read receipt). There is no
    fallback/catch-all handler on purpose, so nothing ever confirms the bot to an
    unauthorized user. The bot token is never logged.
    """
    from telegram.ext import (
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        filters,
    )

    application = ApplicationBuilder().token(config.telegram_bot_token).build()
    application.bot_data["config"] = config
    application.bot_data["triage_lock"] = asyncio.Lock()

    allowed = filters.User(user_id=config.telegram_allowed_user_id)
    application.add_handler(CommandHandler("triage", triage_command, filters=allowed))
    application.add_handler(CommandHandler("start", start_command, filters=allowed))
    application.add_handler(CommandHandler("help", start_command, filters=allowed))
    # T10: a CallbackQueryHandler takes no `filters`, so the pattern only keeps other
    # people's payloads out — the whitelist itself is enforced inside the handler, and
    # with the same silence.
    application.add_handler(
        CallbackQueryHandler(proposal_callback, pattern=rf"^{CALLBACK_PREFIX}:")
    )
    application.add_error_handler(on_error)
    return application


def run_bot(config: Config) -> None:
    """Build the bot and start long-polling (blocking; no webhooks in v0)."""
    application = build_bot(config)
    logger.info("VetTriage bot avviato (long polling). Comando: /triage [ore].")
    # ENABLE_PROPOSALS is optional, so it never shows up in present_keys(): without
    # this line the only way to find out whether the feature is on is to read the
    # .env on the VPS. Not a secret, safe to log.
    logger.info(
        "Fatti di stato (T10, ENABLE_PROPOSALS): %s",
        "attivi" if config.enable_proposals else "spenti",
    )
    application.run_polling()
