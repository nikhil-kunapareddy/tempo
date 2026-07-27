"""Verification without live keys: mock Together + Google Calendar to prove the agent loop
wires together — read tools auto-run, and writes (create_event) are gated behind approval."""

from __future__ import annotations

import json
from types import SimpleNamespace

from backend.app import agent, calendar_tools, together


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_FakeCompletions(responses))


def _msg(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_call(call_id, name, args):
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args))
    )


def _use(monkeypatch, responses):
    """Point together.get_client/get_model at a single shared fake (queue survives resume)."""
    fake = _FakeClient(responses)
    monkeypatch.setattr(together, "get_client", lambda: fake)
    monkeypatch.setattr(together, "get_model", lambda: "test-model")
    return fake


# -- Phase 1: reads auto-run --------------------------------------------------------------
def test_agent_calls_calendar_tool_then_answers(monkeypatch):
    _use(
        monkeypatch,
        [
            _msg(tool_calls=[_tool_call("c1", "list_calendar_events", {"max_results": 5})]),
            _msg(content="You have 1 event Thursday: Standup at 9am."),
        ],
    )
    monkeypatch.setattr(
        calendar_tools,
        "list_calendar_events",
        lambda **kw: {"events": [{"summary": "Standup"}], "count": 1},
    )

    out = agent.run_agent("what's on my calendar Thursday?")

    assert out["status"] == "done"
    assert out["reply"] == "You have 1 event Thursday: Standup at 9am."
    assert out["steps"][0]["tool"] == "list_calendar_events"


def test_agent_direct_answer_without_tool(monkeypatch):
    _use(monkeypatch, [_msg(content="Hi!")])
    out = agent.run_agent("hello")
    assert out["status"] == "done" and out["reply"] == "Hi!" and out["steps"] == []


# -- Phase 2: writes require approval -----------------------------------------------------
def test_create_event_pauses_for_approval_then_executes(monkeypatch):
    _use(
        monkeypatch,
        [
            _msg(tool_calls=[_tool_call("w1", "create_event", {"summary": "Standup", "start": "2026-07-30T09:00:00-07:00", "end": "2026-07-30T09:30:00-07:00"})]),
            _msg(content="Done — I added Standup on Thursday at 9am."),
        ],
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        calendar_tools, "create_event", lambda **kw: calls.append(kw) or {"created": {"summary": kw["summary"]}}
    )

    paused = agent.run_agent("add a standup Thursday 9am")

    # It must NOT have created anything yet — just paused for approval.
    assert paused["status"] == "approval_required"
    assert calls == []
    assert paused["pending"][0]["name"] == "create_event"
    assert "Standup" in paused["pending"][0]["summary"]

    done = agent.resume(paused["messages"], paused["pending"], "approve")

    assert done["status"] == "done"
    assert done["reply"] == "Done — I added Standup on Thursday at 9am."
    assert len(calls) == 1  # executed exactly once, only after approval
    assert done["steps"][0]["approved"] is True


def test_empty_final_reply_surfaces_last_tool_error(monkeypatch):
    # turn 1: read tool that errors; turn 2: model returns EMPTY content
    _use(
        monkeypatch,
        [
            _msg(tool_calls=[_tool_call("c1", "list_calendar_events", {})]),
            _msg(content=""),  # small model returns nothing
        ],
    )
    monkeypatch.setattr(
        calendar_tools, "list_calendar_events", lambda **kw: {"error": "Google Calendar API error 401"}
    )

    out = agent.run_agent("how's my day?")

    assert out["status"] == "done"
    assert "401" in out["reply"]  # not a blank "(no reply)"


def test_provider_auth_error_is_friendly_not_a_crash(monkeypatch):
    import httpx
    import openai

    req = httpx.Request("POST", "https://api.together.xyz/v1/chat/completions")
    boom = openai.AuthenticationError(
        "Invalid API key", response=httpx.Response(401, request=req), body=None
    )

    class _Boom:
        def create(self, **kwargs):
            raise boom

    fake = SimpleNamespace(chat=SimpleNamespace(completions=_Boom()))
    monkeypatch.setattr(together, "get_client", lambda: fake)
    monkeypatch.setattr(together, "get_model", lambda: "test-model")

    out = agent.run_agent("hi")

    assert out["status"] == "error"
    assert "401" in out["error"] and "key" in out["error"].lower()


def test_reject_does_not_execute_the_write(monkeypatch):
    _use(
        monkeypatch,
        [
            _msg(tool_calls=[_tool_call("w1", "create_event", {"summary": "X", "start": "s", "end": "e"})]),
            _msg(content="Okay, I won't add it."),
        ],
    )
    calls: list[dict] = []
    monkeypatch.setattr(calendar_tools, "create_event", lambda **kw: calls.append(kw))

    paused = agent.run_agent("add event X")
    done = agent.resume(paused["messages"], paused["pending"], "reject")

    assert done["status"] == "done"
    assert calls == []  # never executed
    assert done["steps"][0]["approved"] is False
    assert done["reply"] == "Okay, I won't add it."
