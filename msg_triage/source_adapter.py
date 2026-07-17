"""Neutral conversation format and the source-adapter interface.

This module is the boundary that keeps the triage engine independent of any
messaging provider (Callbell today, another BSP tomorrow). Nothing here may
import or reference provider-specific structures: adapters translate their raw
data into these neutral dataclasses, and everything downstream (triage engine,
memory, renderers) speaks only this vocabulary. This is what makes the triage a
portable asset even if the clinic ever leaves Callbell.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol


class Role(str, Enum):
    """Who produced a message, in neutral terms.

    ``NOTA_INTERNA`` is a note handwritten by a colleague (real signal);
    ``NOTA_SISTEMA`` is a provider-generated note such as an assignment event.
    They are kept distinct so the triage never mistakes automation for a human
    remark.
    """

    CLIENTE = "CLIENTE"
    OPERATORE = "OPERATORE"
    NOTA_INTERNA = "NOTA_INTERNA"
    NOTA_SISTEMA = "NOTA_SISTEMA"


@dataclass(frozen=True)
class Message:
    """A single message in a conversation, provider-agnostic."""

    role: Role
    text: str
    timestamp: datetime  # timezone-aware (UTC)


@dataclass(frozen=True)
class Conversation:
    """A conversation in the neutral format the triage engine consumes.

    ``assigned_user`` is the e-mail of the operator the conversation is assigned
    to (or ``None``): a PRESIDIO signal. ``messages`` are in chronological order
    (oldest first).
    """

    contact_id: str
    name: str
    channel: str
    tags: tuple[str, ...]
    assigned_user: str | None
    messages: tuple[Message, ...]


class SourceAdapter(Protocol):
    """Contract every conversation source must implement.

    Returns recent conversations already in the neutral format. Implementations
    decide how to talk to their provider; callers never see provider specifics.
    """

    def fetch_recent_conversations(
        self, window_hours: float = 6.0
    ) -> list[Conversation]:
        """Return conversations with activity within the last ``window_hours``."""
        ...
