"""Unit tests for the telemetry wrapper. No network, no mock library.

The library is installed separately (pinned to a tag, out of ``pyproject.toml``), so
these tests must pass whether or not it is present: they replace the module globals
with hand-rolled recorders, the same dependency-injection style as the other tests.

What matters operationally is the fail-silent contract, so that is what is tested:
nothing is emitted while inactive, nothing propagates when the library misbehaves,
and no call ever leaves ``tenant_id`` to chance.
"""

from __future__ import annotations

import asyncio

import pytest

from msg_triage import telemetry


class Recorder:
    """Collects the calls the wrapper makes to the library."""

    def __init__(self, *, boom: bool = False) -> None:
        self.calls: list[tuple] = []
        self._boom = boom

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._boom:
            raise RuntimeError("supabase è esploso")


@pytest.fixture
def active(monkeypatch):
    """Wire a fully active telemetry over recorders; returns them by name."""
    recorders = {name: Recorder() for name in ("event", "usage", "heartbeat", "init")}
    monkeypatch.setattr(telemetry, "_log_event", recorders["event"])
    monkeypatch.setattr(telemetry, "_log_usage", recorders["usage"])
    monkeypatch.setattr(telemetry, "_heartbeat", recorders["heartbeat"])
    monkeypatch.setattr(telemetry, "_init", recorders["init"])
    monkeypatch.setattr(telemetry, "_active", True)
    monkeypatch.setattr(telemetry, "_crashed", False)
    return recorders


# --- The guard: inactive telemetry touches nothing ------------------------------


def test_inactive_emits_nothing(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(telemetry, "_log_event", recorder)
    monkeypatch.setattr(telemetry, "_log_usage", recorder)
    monkeypatch.setattr(telemetry, "_heartbeat", recorder)
    monkeypatch.setattr(telemetry, "_active", False)
    monkeypatch.setattr(telemetry, "_crashed", False)

    telemetry.event("processing_started")
    telemetry.usage(model="m", operation="o", input_tokens=1, output_tokens=2)
    telemetry.shutdown()
    telemetry.crashed(RuntimeError("x"))
    asyncio.run(telemetry.aevent("processing_started"))

    assert recorder.calls == []


def test_setup_without_library_is_a_no_op(monkeypatch):
    """The library is optional: its absence must not raise nor activate anything."""
    monkeypatch.setattr(telemetry, "_init", None)
    monkeypatch.setattr(telemetry, "_active", False)

    telemetry.setup()

    assert telemetry._active is False


def test_setup_activates_with_the_agent_id(active, monkeypatch):
    monkeypatch.setattr(telemetry, "_active", False)

    telemetry.setup()

    assert active["init"].calls == [((), {"agent_id": "msg-triage"})]
    assert telemetry._active is True


def test_setup_survives_a_failing_init(monkeypatch):
    monkeypatch.setattr(telemetry, "_init", Recorder(boom=True))
    monkeypatch.setattr(telemetry, "_active", False)

    telemetry.setup()  # must not raise

    assert telemetry._active is False


# --- Fail-silent: a broken library never reaches the caller ---------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: telemetry.event("processing_started"),
        lambda: telemetry.usage(
            model="m", operation="o", input_tokens=1, output_tokens=2
        ),
        lambda: telemetry.shutdown(),
        lambda: telemetry.crashed(RuntimeError("x")),
        lambda: asyncio.run(telemetry.aevent("processing_started")),
    ],
)
def test_library_failures_never_propagate(monkeypatch, call):
    boom = Recorder(boom=True)
    monkeypatch.setattr(telemetry, "_log_event", boom)
    monkeypatch.setattr(telemetry, "_log_usage", boom)
    monkeypatch.setattr(telemetry, "_heartbeat", boom)
    monkeypatch.setattr(telemetry, "_active", True)
    monkeypatch.setattr(telemetry, "_crashed", False)

    call()  # the whole point: no exception escapes


# --- Events ---------------------------------------------------------------------


def test_event_always_sends_tenant_self(active):
    telemetry.event("conversations_fetched", metadata={"n_conversations": 3})

    (args, kwargs) = active["event"].calls[0]
    assert args == ("conversations_fetched",)
    assert kwargs["tenant_id"] == "self"
    assert kwargs["severity"] == "info"
    assert kwargs["metadata"] == {"n_conversations": 3}


def test_aevent_forwards_off_the_event_loop(active):
    async def scenario():
        await telemetry.aevent("processing_failed", severity="error")

    asyncio.run(scenario())

    (args, kwargs) = active["event"].calls[0]
    assert args == ("processing_failed",)
    assert kwargs["severity"] == "error"
    assert kwargs["tenant_id"] == "self"


# --- Usage ----------------------------------------------------------------------


def test_usage_sends_anthropic_and_no_cost(active):
    telemetry.usage(
        model="claude-opus-4-8",
        operation="conversation_triage",
        input_tokens=1200,
        output_tokens=340,
    )

    (_, kwargs) = active["usage"].calls[0]
    assert kwargs["provider"] == "anthropic"
    assert kwargs["model"] == "claude-opus-4-8"
    assert kwargs["operation"] == "conversation_triage"
    assert kwargs["tenant_id"] == "self"
    assert kwargs["input_tokens"] == 1200
    assert kwargs["output_tokens"] == 340
    # The pricing table is the single source of truth for cost.
    assert "cost_usd" not in kwargs


def test_usage_skips_when_no_token_counts(active):
    """Injected fake clients have no usage on their response: emit nothing."""
    telemetry.usage(
        model="claude-opus-4-8",
        operation="conversation_triage",
        input_tokens=None,
        output_tokens=None,
    )

    assert active["usage"].calls == []


def test_usage_emits_with_only_one_count(active):
    telemetry.usage(
        model="claude-opus-4-8",
        operation="conversation_triage",
        input_tokens=None,
        output_tokens=340,
    )

    assert len(active["usage"].calls) == 1


# --- Process lifecycle ----------------------------------------------------------


def test_shutdown_reports_a_clean_stop(active):
    telemetry.shutdown()

    (args, kwargs) = active["event"].calls[0]
    assert args == ("agent_stopped",)
    assert kwargs["metadata"] == {"reason": "process_exit"}
    assert active["heartbeat"].calls == [(("offline",), {})]


def test_crashed_reports_the_class_and_goes_offline(active):
    telemetry.crashed(ValueError("dettaglio che non deve viaggiare"))

    (args, kwargs) = active["event"].calls[0]
    assert args == ("agent_crashed",)
    assert kwargs["severity"] == "error"
    assert kwargs["metadata"] == {"exception": "ValueError"}
    # Red dot immediately, without waiting for the missing-heartbeat timeout.
    assert active["heartbeat"].calls == [(("offline",), {})]


def test_shutdown_after_crash_adds_nothing(active):
    """One closing event per process life: a crash must not also look like a stop."""
    telemetry.crashed(ValueError("boom"))
    telemetry.shutdown()

    types = [args[0] for (args, _) in active["event"].calls]
    assert types == ["agent_crashed"]
    assert len(active["heartbeat"].calls) == 1
