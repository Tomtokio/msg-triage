"""Probe: do Callbell's two views of the same contact agree?

The neutral format is built from the CONTACT LIST: ``iter_contacts()`` walks ``/contacts``
and ``_build_conversation`` reads ``name`` and ``tags`` straight off each item. Every T10
tag decision then rests on that ``tags`` — "the contact already carries ``Ricoverato``" is
the gate on every add, and it is compared byte for byte.

What prompted this: a triage run showed ``Alessandra 🦥🌈🎵🎶`` for a contact whose direct
``GET /contacts/:uuid`` calls it ``Alessandra Fabbri``. Same uuid, two names. The ``~`` on
another entry ("~Leo Di Curzio") points the same way — the list looks like it carries the
WhatsApp-side profile and the GET the stored record. If the two views disagree on ``name``
they can disagree on ``tags``, and then T10 is deciding on the wrong one.

This probe does NOT settle which view should win. That is a call to make with the data in
hand: the list gives a whole page in one request, the GET costs one call per contact, and
"fresher" is worth paying for only if the two actually differ. What it settles is whether
they differ at all, and how widely.

Esito (2026-08-15): girato sul contatto che aveva dato il sospetto — le due viste coincidono
su tutti i campi, ``name`` e ``tags`` inclusi. Quel nome discordante era una rinomina avvenuta
fra le due letture, non una divergenza strutturale, e la lista resta la base di ``convo.tags``.
Il dettaglio sta in docs/dev_notes.md, "Le due viste di un contatto".

Read-only by construction: the client is built without ``allow_writes``, so a write here is
impossible rather than merely unintended.

Usage (from the repo root, with CALLBELL_API_KEY in the environment or .env):
    .venv/bin/python scripts/probe_contact_view.py --contact <uuid>
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from msg_triage.callbell_adapter import CallbellClient, CallbellError

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# What the neutral format actually reads off a list item (callbell_adapter._build_
# conversation). `tags` is the one that decides T10's behaviour; `name` is the one that
# gave the divergence away.
NEUTRAL_FIELDS = ("name", "tags")

# `/contacts` is ~332 pages, ordered by recent activity. A contact that appeared in a
# triage window sits at the very top, so proving a negative by scanning everything would
# cost far more than the answer is worth.
DEFAULT_MAX_CONTACTS = 200


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(dotenv_path=_PROJECT_ROOT / ".env", override=False)


def find_in_list(
    client: CallbellClient, contact_uuid: str, *, max_contacts: int
) -> tuple[dict | None, int]:
    """The contact as the LIST view shows it, plus how many contacts were scanned."""
    scanned = 0
    for contact in client.iter_contacts():
        scanned += 1
        if contact.get("uuid") == contact_uuid:
            return contact, scanned
        if scanned >= max_contacts:
            break
    return None, scanned


def differing_fields(list_view: dict, get_view: dict) -> list[str]:
    """Every key the two views disagree on.

    The union of the keys, not the intersection: a field one view omits entirely is part
    of the divergence, and it is exactly the shape a stale or denormalised copy takes.
    Absence and ``null`` read the same here — that is the right call in the direction that
    matters, since a tag list that is missing is not a tag list that is empty.
    """
    return sorted(
        key
        for key in set(list_view) | set(get_view)
        if list_view.get(key) != get_view.get(key)
    )


def report(list_view: dict, get_view: dict, *, scanned: int, calls: int) -> None:
    """Print both views side by side, then the verdict, in that order.

    Everything goes through ``repr()``: a trailing space, a non-breaking space or a
    lowercase initial is the entire difference between a tag T10 recognises and one it
    does not, and printed plain they are indistinguishable from the real thing.
    """
    print(f"\nTrovato nella lista dopo {scanned} contatti ({calls} chiamate API).\n")

    print("I campi che il formato neutro legge DALLA LISTA:")
    for field in NEUTRAL_FIELDS:
        print(f"\n  {field}")
        print(f"    lista  {list_view.get(field)!r}")
        print(f"    GET    {get_view.get(field)!r}")

    differing = differing_fields(list_view, get_view)
    print("\n" + "=" * 70)
    if not differing:
        print("Le due viste coincidono su TUTTI i campi.")
        print(
            "Se il nome discordava al momento del triage, allora è cambiato fra i due\n"
            "momenti (rinomina lato WhatsApp o lato Callbell), non per una divergenza\n"
            "strutturale fra le viste."
        )
        return

    print(f"Le due viste DIVERGONO su {len(differing)} campi: {differing!r}")
    if "tags" in differing:
        print(
            "\n⚠️  DIVERGONO SUI TAG. È il campo su cui T10 decide: `convo.tags` viene\n"
            "    dalla lista, e ci sopra passa il gate di ogni aggiunta di tag. Da qui\n"
            "    la domanda che resta aperta: quale delle due viste è la verità."
        )
    else:
        print(
            "\nSui TAG le due viste concordano: il gate di T10 legge lo stesso valore\n"
            "da entrambe le parti, oggi e su questo contatto."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contact",
        required=True,
        metavar="UUID",
        help="uuid del contatto da confrontare fra le due viste",
    )
    parser.add_argument(
        "--max-contatti",
        dest="max_contacts",
        type=int,
        default=DEFAULT_MAX_CONTACTS,
        metavar="N",
        help=(
            "quanti contatti scorrere nella lista prima di arrendersi "
            f"(default: {DEFAULT_MAX_CONTACTS}; la lista è ordinata per attività recente)"
        ),
    )
    args = parser.parse_args()

    _load_dotenv()
    # Only Callbell is needed: this probe must not require an Anthropic/Telegram/Supabase
    # setup to run. Same reasoning as probe_rename.py and cleanup_stale_tags.py.
    api_key = (os.environ.get("CALLBELL_API_KEY") or "").strip()
    if not api_key:
        print("Manca CALLBELL_API_KEY (nell'ambiente o nel .env).", file=sys.stderr)
        return 1

    # No allow_writes: this client cannot write, it is not merely expected not to.
    client = CallbellClient(api_key)
    try:
        get_view = client.get_contact(args.contact)
        list_view, scanned = find_in_list(
            client, args.contact, max_contacts=args.max_contacts
        )
    except CallbellError as exc:
        print(f"Callbell ha risposto male: {exc}", file=sys.stderr)
        return 1

    if list_view is None:
        print(
            f"\nContatto non trovato nei primi {scanned} contatti della lista.\n"
            "La lista è ordinata per attività recente: se questo contatto non scrive da\n"
            "un po', rilancia con --max-contatti più alto.",
            file=sys.stderr,
        )
        return 1

    report(list_view, get_view, scanned=scanned, calls=client.request_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
