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

Design: python-telegram-bot v21+ is async, but the pipeline (requests + anthropic)
is blocking, so the heavy work runs off the event loop via ``asyncio.to_thread``.
The heavy logic lives in pure sync functions (unit-testable in the house style with
injected fakes); the async handlers are thin glue. ``telegram`` is imported lazily
inside the factory/launcher/error-handler, so importing this module (e.g. to test
the pure helpers) needs no telegram install and stays light.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from typing import TYPE_CHECKING

from msg_triage import telemetry
from msg_triage.callbell_adapter import CallbellError, build_adapter
from msg_triage.config import Config
from msg_triage.renderers import RenderedTriage, render_all
from msg_triage.source_adapter import Conversation
from msg_triage.storage import save_triage_run
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

        # T7: best-effort, after delivery, off the event loop. Cannot raise.
        await asyncio.to_thread(
            save_triage_run, config, result, rendered, conversations, window_hours=hours
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start`` and ``/help`` (whitelisted): show the usage."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "Triage delle conversazioni WhatsApp della clinica (sola lettura).\n"
        "• /triage — ultime 6 ore\n"
        "• /triage 12 — ultime 12 ore\n"
        "Rispondo con tre messaggi: schema, tabella e vocale (sintesi)."
    )


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
    from telegram.ext import ApplicationBuilder, CommandHandler, filters

    application = ApplicationBuilder().token(config.telegram_bot_token).build()
    application.bot_data["config"] = config
    application.bot_data["triage_lock"] = asyncio.Lock()

    allowed = filters.User(user_id=config.telegram_allowed_user_id)
    application.add_handler(CommandHandler("triage", triage_command, filters=allowed))
    application.add_handler(CommandHandler("start", start_command, filters=allowed))
    application.add_handler(CommandHandler("help", start_command, filters=allowed))
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
