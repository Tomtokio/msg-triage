"""Manual smoke test for the triage engine against the REAL Anthropic API.

Read-only: it never replies to anyone. It builds a few synthetic neutral
conversations (default) or pulls real ones from Callbell, runs one triage, and
prints the structured result. Use it to calibrate the prompt on real data
(clinical-urgency threshold, promise conservatism). NOT part of the pytest suite.

Usage (from the repo root, with a populated .env):
    .venv/bin/python scripts/smoke_triage.py            # synthetic conversations
    .venv/bin/python scripts/smoke_triage.py --real 6   # last 6h from Callbell

The T10 A/B, which is the acceptance gate for the facts block — same window, three
runs, and gruppo/urgenza/presidio/temperatura/motivo must not move between them:
    .venv/bin/python scripts/smoke_triage.py --real 6 --no-facts   # the reference
    .venv/bin/python scripts/smoke_triage.py --real 6 --facts      # twice, minutes apart
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from msg_triage.config import ConfigError, load_config
from msg_triage.logging_setup import setup_logging
from msg_triage.renderers import render_all
from msg_triage.source_adapter import Conversation, Message, Role
from msg_triage.triage_engine import TriageResult, build_triage_engine, extract_species

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


def _format_fatti(fatti) -> str:
    """One dense line per entry: the A/B is read by eye, side by side."""
    animali = (
        "[" + ", ".join(f"{a.specie or '?'} {a.nome or '?'}" for a in fatti.animali) + "]"
        if fatti.animali
        else "—"
    )
    if fatti.dimissione is None:
        dimissione = "—"
    else:
        dimissione = f"{fatti.dimissione.stato.value} {fatti.dimissione.quando or '(data ignota)'}"
    return (
        f"ricovero={fatti.ricovero.value}  dimissione={dimissione}  "
        f"animali={animali}  proprietario={fatti.proprietario or '—'}"
    )


def _print_result(result: TriageResult, *, facts_on: bool) -> None:
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
        if entry.fatti is not None:
            print(f"  fatti: {_format_fatti(entry.fatti)}")

    # The species hit-rate, printed in a form you can compare at a glance between an
    # OFF run and an ON run. Asking for animali[].specie in a structured field can
    # make the model stop marking **specie** in the prose, and that marker is the
    # ONLY source of the specie column: a silent drop here is a real regression.
    marked = sum(1 for entry in result.conversations if extract_species(entry) is not None)
    label = "ON" if facts_on else "OFF"
    print(f"\n[specie marcate — fatti {label}: {marked}/{len(result.conversations)}]")


def _print_rendered(result: TriageResult) -> None:
    """Show the three T5 formats. The vocale char count is printed on purpose: it
    is the thing to watch — if stato_sintetico is long, the voice reads long too."""
    rendered = render_all(result)
    for title, text in (
        ("SCHEMA", rendered.schema_text),
        ("TABELLA", rendered.table_text),
        ("VOCALE", rendered.vocal_text),
    ):
        print(f"\n========== {title} ==========\n{text}")
    print(f"\n[vocale: {len(rendered.vocal_text)} caratteri]")


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
    parser.add_argument(
        "--facts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="force the T10 facts extraction on (--facts) or off (--no-facts); "
        "default follows ENABLE_PROPOSALS. Use both, on the same window, for the A/B",
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

    facts_on = config.enable_proposals if args.facts is None else args.facts
    print(f"Fatti di stato (T10): {'ON' if facts_on else 'OFF'}.")

    engine = build_triage_engine(config, extract_facts=facts_on)
    result = engine.triage(conversations)
    _print_result(result, facts_on=facts_on)
    _print_rendered(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
