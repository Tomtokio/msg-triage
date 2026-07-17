"""Manual smoke test for the triage engine against the REAL Anthropic API.

Read-only: it never replies to anyone. It builds a few synthetic neutral
conversations (default) or pulls real ones from Callbell, runs one triage, and
prints the structured result. Use it to calibrate the prompt on real data
(clinical-urgency threshold, promise conservatism). NOT part of the pytest suite.

Usage (from the repo root, with a populated .env):
    .venv/bin/python scripts/smoke_triage.py            # synthetic conversations
    .venv/bin/python scripts/smoke_triage.py --real 6   # last 6h from Callbell
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from msg_triage.config import ConfigError, load_config
from msg_triage.logging_setup import setup_logging
from msg_triage.source_adapter import Conversation, Message, Role
from msg_triage.triage_engine import TriageResult, build_triage_engine

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=False)


def _synthetic_conversations() -> list[Conversation]:
    now = datetime.now(timezone.utc)
    ago = lambda m: now - timedelta(minutes=m)  # noqa: E731 - terse helper for a script
    return [
        Conversation(
            contact_id="demo-rossi",
            name="Sig.ra Rossi",
            channel="whatsapp",
            tags=(),
            assigned_user=None,
            messages=(
                Message(Role.CLIENTE, "Buongiorno, il mio coniglio non mangia da ieri sera e sta fermo in un angolo.", ago(200)),
                Message(Role.CLIENTE, "C'è qualcuno? Sono preoccupata.", ago(35)),
            ),
        ),
        Conversation(
            contact_id="demo-bianchi",
            name="Sig.ra Bianchi",
            channel="whatsapp",
            tags=("dimissione",),
            assigned_user="giulia@clinica.it",
            messages=(
                Message(Role.CLIENTE, "Il coniglio può essere dimesso oggi?", ago(120)),
                Message(Role.OPERATORE, "Le confermo entro due ore per stasera.", ago(90)),
            ),
        ),
        Conversation(
            contact_id="demo-verdi",
            name="Sig. Verdi",
            channel="whatsapp",
            tags=(),
            assigned_user="martina@clinica.it",
            messages=(
                Message(Role.CLIENTE, "A che ora aprite sabato?", ago(300)),
                Message(Role.OPERATORE, "Sabato 9-13. A presto!", ago(295)),
            ),
        ),
    ]


def _print_result(result: TriageResult) -> None:
    if not result.conversations:
        print("(nessuna voce di triage)")
        return
    for entry in result.conversations:
        print(f"\n[{entry.gruppo.value.upper()}] {entry.nome} ({entry.contact_id})")
        print(f"  urgenza={entry.urgenza.value}  presidio={entry.presidio.value}  temperatura={entry.temperatura.value}")
        print(f"  motivo: {entry.motivo}")
        print(f"  stato: {entry.stato_sintetico}")
        if entry.azione_suggerita:
            print(f"  azione: {entry.azione_suggerita}")
        if entry.promessa_rilevata is not None:
            p = entry.promessa_rilevata
            print(f"  promessa: {p.testo!r} (scadenza stimata: {p.scadenza_stimata})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--real",
        type=float,
        nargs="?",
        const=6.0,
        default=None,
        metavar="HOURS",
        help="pull real conversations from Callbell for the last HOURS (default 6) instead of synthetic ones",
    )
    args = parser.parse_args()

    _load_dotenv()
    setup_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if args.real is not None:
        from msg_triage.callbell_adapter import build_adapter

        conversations = build_adapter(config).fetch_recent_conversations(window_hours=args.real)
        print(f"Fetched {len(conversations)} conversation(s) from Callbell (last {args.real}h).")
    else:
        conversations = _synthetic_conversations()
        print(f"Using {len(conversations)} synthetic conversation(s).")

    engine = build_triage_engine(config)
    result = engine.triage(conversations)
    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
