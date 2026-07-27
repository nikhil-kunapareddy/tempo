"""Google Calendar — the only integration. Tools: list events, get one event (reads), and
create an event (write). Writes are marked in WRITE_TOOLS so the agent loop can gate them
behind user approval. Auth is a Google OAuth access token the user pastes in Settings
(Phase 3 will add a real OAuth flow with refresh)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from . import google_oauth

GCAL_BASE = "https://www.googleapis.com/calendar/v3"

# Tools that MODIFY the calendar — the agent must get user approval before running these.
WRITE_TOOLS = {"create_event"}

LIST_EVENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_calendar_events",
        "description": (
            "List events from the user's primary Google Calendar within an optional time "
            "range. Use this to answer questions about schedule/availability. Times are "
            "ISO-8601 (e.g. 2026-07-27T00:00:00Z)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "ISO-8601 start, inclusive. Defaults to now."},
                "time_max": {"type": "string", "description": "ISO-8601 end, exclusive. Optional."},
                "max_results": {"type": "integer", "description": "Max events (1-50)."},
            },
        },
    },
}

GET_EVENT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_event",
        "description": "Fetch a single event from the primary calendar by its event id.",
        "parameters": {
            "type": "object",
            "properties": {"event_id": {"type": "string", "description": "The Google Calendar event id."}},
            "required": ["event_id"],
        },
    },
}

CREATE_EVENT_TOOL = {
    "type": "function",
    "function": {
        "name": "create_event",
        "description": (
            "Create an event on the user's primary Google Calendar. This MODIFIES the "
            "calendar, so it will be shown to the user for approval before it runs. Provide "
            "start/end as ISO-8601 WITH a timezone offset (e.g. 2026-07-30T09:00:00-07:00), "
            "or pass a `timezone` IANA name (e.g. America/Los_Angeles)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title."},
                "start": {"type": "string", "description": "ISO-8601 start datetime."},
                "end": {"type": "string", "description": "ISO-8601 end datetime."},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "timezone": {"type": "string", "description": "IANA tz name, if start/end have no offset."},
            },
            "required": ["summary", "start", "end"],
        },
    },
}

TOOLS = [LIST_EVENTS_TOOL, GET_EVENT_TOOL, CREATE_EVENT_TOOL]


def _token() -> str:
    # A fresh access token (auto-refreshed via the stored refresh token when needed).
    return google_oauth.get_valid_access_token()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def list_calendar_events(
    time_min: str | None = None, time_max: str | None = None, max_results: int = 10
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": max(1, min(int(max_results or 10), 50)),
        "timeMin": time_min or dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if time_max:
        params["timeMax"] = time_max
    try:
        resp = httpx.get(f"{GCAL_BASE}/calendars/primary/events", headers=_headers(), params=params, timeout=20)
    except Exception as exc:
        return {"error": f"Couldn't reach Google Calendar: {exc.__class__.__name__}"}
    if resp.status_code >= 400:
        return {"error": f"Google Calendar API error {resp.status_code}: {resp.text[:200]}"}
    items = resp.json().get("items", [])
    events = [_summarize(it) for it in items]
    return {"events": events, "count": len(events)}


def get_event(event_id: str) -> dict[str, Any]:
    try:
        resp = httpx.get(
            f"{GCAL_BASE}/calendars/primary/events/{event_id}", headers=_headers(), timeout=20
        )
    except Exception as exc:
        return {"error": f"Couldn't reach Google Calendar: {exc.__class__.__name__}"}
    if resp.status_code >= 400:
        return {"error": f"Google Calendar API error {resp.status_code}: {resp.text[:200]}"}
    return {"event": _summarize(resp.json())}


def create_event(
    summary: str,
    start: str,
    end: str,
    description: str | None = None,
    location: str | None = None,
    timezone: str | None = None,
) -> dict[str, Any]:
    start_obj: dict[str, Any] = {"dateTime": start}
    end_obj: dict[str, Any] = {"dateTime": end}
    if timezone:
        start_obj["timeZone"] = timezone
        end_obj["timeZone"] = timezone
    body: dict[str, Any] = {"summary": summary, "start": start_obj, "end": end_obj}
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    try:
        resp = httpx.post(
            f"{GCAL_BASE}/calendars/primary/events", headers=_headers(), json=body, timeout=20
        )
    except Exception as exc:
        return {"error": f"Couldn't reach Google Calendar: {exc.__class__.__name__}"}
    if resp.status_code >= 400:
        return {"error": f"Google Calendar API error {resp.status_code}: {resp.text[:200]}"}
    created = resp.json()
    return {"created": _summarize(created), "htmlLink": created.get("htmlLink")}


def _summarize(it: dict[str, Any]) -> dict[str, Any]:
    start, end = it.get("start", {}), it.get("end", {})
    return {
        "id": it.get("id"),
        "summary": it.get("summary", "(no title)"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "location": it.get("location"),
    }


def describe_call(name: str, args: dict[str, Any]) -> str:
    """Human-readable one-liner for an approval prompt."""
    if name == "create_event":
        when = args.get("start", "?")
        return f"Create event “{args.get('summary', '(untitled)')}” at {when}"
    return f"{name}({', '.join(f'{k}={v}' for k, v in args.items())})"


def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool call from the model to its implementation."""
    if name == "list_calendar_events":
        allowed = {k: v for k, v in args.items() if k in ("time_min", "time_max", "max_results")}
        return list_calendar_events(**allowed)
    if name == "get_event":
        return get_event(str(args.get("event_id", "")))
    if name == "create_event":
        allowed = {
            k: v
            for k, v in args.items()
            if k in ("summary", "start", "end", "description", "location", "timezone")
        }
        return create_event(**allowed)
    return {"error": f"unknown tool: {name}"}
