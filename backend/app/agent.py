"""The agentic loop: send the conversation to Together, let the model call Google Calendar
tools, feed results back, and repeat until a final answer.

Human-in-the-loop: READ tools (list/get) run automatically, but WRITE tools (create_event)
are gated. When the model asks to write, the loop pauses and returns `approval_required`
with the pending action + the transcript. The UI shows Approve/Reject; `resume()` continues
the loop with the write executed (approve) or declined (reject)."""

from __future__ import annotations

import json
from typing import Any

import openai

from . import calendar_tools, together

SYSTEM_PROMPT = (
    "You are Tempo, a helpful personal assistant running locally on the user's Mac. You can read the "
    "user's Google Calendar (list_calendar_events, get_event) and propose new events "
    "(create_event). When asked about the schedule, call a read tool and answer concisely with "
    "specific dates/times. To add something, call create_event — the user will be asked to "
    "approve it before it is created, so state clearly what you intend to create. If a tool "
    "returns an error, explain it plainly and suggest a fix."
)

MAX_STEPS = 6  # cap tool round-trips so a loop can't run away


def _friendly_provider_error(exc: openai.OpenAIError) -> str:
    """Turn a Together/OpenAI SDK error into a message the user can act on."""
    if isinstance(exc, openai.AuthenticationError):
        return (
            "Together AI rejected your API key (401). Update it in Settings — "
            "get a key at https://api.together.ai/settings/api-keys."
        )
    if isinstance(exc, openai.APIConnectionError):
        return "Couldn't reach Together AI — check your network and try again."
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code == 404:
            return "Together AI returned 404 — the model id looks invalid. Pick a model in Settings."
        if exc.status_code == 429:
            return "Together AI rate limit or quota reached (429). Try again shortly."
        if exc.status_code == 400:
            return (
                "Together can't serve that model (400) — it may need a dedicated (non-serverless) "
                "endpoint or isn't on your plan. Pick a different model from the Settings dropdown."
            )
        return f"Together AI error ({exc.status_code}). Check your Settings and try again."
    return f"Together AI error: {exc}"


def _exec(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a tool, turning ANY failure (bad args, calendar not connected, API error) into an
    error result the model can see and relay — never an unhandled 500."""
    try:
        return calendar_tools.execute_tool(name, args)
    except Exception as exc:
        return {"error": f"{name} failed: {exc}"}


def run_agent(message: str, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Start a turn. Returns either {status:'done', reply, steps} or
    {status:'approval_required', pending, messages, steps}."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": message})
    return _run(messages, [])


def resume(
    messages: list[dict[str, Any]], pending: list[dict[str, Any]], decision: str
) -> dict[str, Any]:
    """Continue a paused turn after the user approves/rejects the pending write(s)."""
    steps: list[dict[str, Any]] = []
    approved = decision == "approve"
    for call in pending:
        if approved:
            result = _exec(call["name"], call.get("args") or {})
        else:
            result = {"declined": True, "note": "The user declined this action; do not retry it."}
        steps.append({"tool": call["name"], "args": call.get("args"), "result": result, "approved": approved})
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})
    return _run(messages, steps)


def _run(messages: list[dict[str, Any]], steps: list[dict[str, Any]]) -> dict[str, Any]:
    client = together.get_client()
    model = together.get_model()

    for _ in range(MAX_STEPS):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=calendar_tools.TOOLS
            )
        except openai.OpenAIError as exc:  # bad key, bad model, rate limit, network…
            return {"status": "error", "reply": None, "error": _friendly_provider_error(exc), "steps": steps}
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []

        assistant_entry: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if tool_calls:
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ]
        messages.append(assistant_entry)

        if not tool_calls:
            reply = (msg.content or "").strip()
            if not reply:
                # Some small models return an empty final message. Don't show a blank bubble —
                # surface the most recent tool error, or a hint to try again / use a stronger model.
                last_error = next(
                    (
                        s["result"]["error"]
                        for s in reversed(steps)
                        if isinstance(s.get("result"), dict) and s["result"].get("error")
                    ),
                    None,
                )
                reply = last_error or (
                    "I couldn't produce a response. Try rephrasing, or pick a stronger "
                    "model in Settings."
                )
            return {"status": "done", "reply": reply, "steps": steps}

        pending_writes: list[dict[str, Any]] = []
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if name in calendar_tools.WRITE_TOOLS:
                # gate: don't execute — collect for approval
                pending_writes.append({"id": tc.id, "name": name, "args": args})
            else:
                result = _exec(name, args)
                steps.append({"tool": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})

        if pending_writes:
            # pause: the write tool_calls have no results yet; resume() supplies them
            return {
                "status": "approval_required",
                "pending": [
                    {**w, "summary": calendar_tools.describe_call(w["name"], w["args"])}
                    for w in pending_writes
                ],
                "messages": messages,
                "steps": steps,
            }

    return {"status": "done", "reply": "(stopped: reached the tool-call limit)", "steps": steps}
