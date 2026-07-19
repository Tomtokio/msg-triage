"""T8 — Telegram bot: command interface and delivery for the triage.

Read-only toward clients: it never replies to WhatsApp, it only delivers the
triage to the authorized operator on Telegram. It orchestrates the working
pipeline T2 (fetch) -> T3 (triage) -> T5 (render) and sends the three formats as
three distinct messages (dev_notes: never one block).

Not wired yet (seams in place, no rework when they land):
- T4 memory: ``triage`` is called without ``previous_state``; ``_memory_clause``
  in the renderers still returns ``""``.
- T7 persistence: nothing is saved.
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
from typing import TYPE_CHECKING

from msg_triage.callbell_adapter import CallbellError, build_adapter
from msg_triage.config import Config
from msg_triage.renderers import RenderedTriage, render_all
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


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` characters.

    Prefers newline boundaries so lines stay intact; a single line longer than
    ``limit`` is hard-split as a last resort. Never returns an empty list (an empty
    string yields one empty chunk). Needed because the schema/table (full giornale
    di bordo) can exceed Telegram's per-message limit.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            # Flush what we have, then hard-split the overlong line.
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                piece = line[i : i + limit]
                if len(piece) == limit:
                    chunks.append(piece)
                else:
                    current = piece  # remainder seeds the next chunk
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


def run_triage_pipeline(
    config: Config,
    hours: float,
    *,
    adapter=None,
    engine=None,
) -> tuple[TriageResult, RenderedTriage]:
    """Run the synchronous T2 -> T3 -> T5 pipeline; return the result + rendering.

    Blocking (requests + anthropic): the async handler runs it via
    ``asyncio.to_thread``. ``adapter``/``engine`` are injectable for tests; when
    omitted they are built from ``config``. Memory (T4) and persistence (T7) are not
    wired — ``triage`` is called without ``previous_state`` and nothing is saved.
    """
    adapter = adapter if adapter is not None else build_adapter(config)
    conversations = adapter.fetch_recent_conversations(window_hours=hours)
    engine = engine if engine is not None else build_triage_engine(config)
    result = engine.triage(conversations)  # SEAM T4: previous_state intentionally omitted
    rendered = render_all(result)
    return result, rendered


def _hours_label(hours: float) -> str:
    """Human-facing Italian label for a window, e.g. "1 ora" / "12 ore" / "6.5 ore"."""
    return "1 ora" if hours == 1 else f"{hours:g} ore"


# --- Async handlers (thin glue over the pure core) -----------------------------


async def _send(message, text: str) -> None:
    """Send possibly-long plain text as one or more Telegram messages (no markup)."""
    for chunk in split_message(text):
        await message.reply_text(chunk)


async def _deliver_triage(message, rendered: RenderedTriage) -> None:
    """Send the three formats as three distinct plain-text messages (each chunked)."""
    await _send(message, f"📋 SCHEMA\n\n{rendered.schema_text}")
    await _send(message, f"🧾 TABELLA\n\n{rendered.table_text}")
    # SEAM T6: when the TTS lands, the "vocale" becomes an audio file here instead
    # of text; nothing else in the pipeline changes.
    await _send(message, f"🔊 VOCALE\n\n{rendered.vocal_text}")


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
        await message.reply_text(
            f"🔍 Recupero le conversazioni delle ultime {label} e le analizzo…"
        )
        try:
            result, rendered = await asyncio.to_thread(run_triage_pipeline, config, hours)
        except (CallbellError, TriageError) as exc:
            logger.warning("Triage fallito: %s", exc)
            await message.reply_text(f"⚠️ Errore durante il triage: {exc}")
            return
        except Exception:  # noqa: BLE001 - last resort; the user must not be left hanging
            logger.exception("Errore imprevisto durante il triage")
            await message.reply_text(
                "⚠️ Errore imprevisto durante il triage. Controlla i log."
            )
            return

        if not result.conversations:
            await message.reply_text(
                f"✅ Nessuna conversazione con attività nelle ultime {label}."
            )
            return

        await _deliver_triage(message, rendered)


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
    application.run_polling()
