"""Outreach: a table of recruiters, a resume and a sample email → one Gmail draft per row.

The user fills in the table (org, role, recruiter name and email), uploads a resume and pastes
an email they've written before. Claude Code (claude_code.py) rewrites that email for each row;
the user reviews and edits the drafts here, then saves them to Gmail's Drafts folder. Nothing is
ever sent.

State is one JSON file, ~/.tempo/outreach/state.json, so a half-finished batch survives a
restart. Up to three generations run at once (the UI's limit), and each one finishes by writing
its draft back, so every read-modify-write of the file holds `_lock`. The sections it guards
never await, which is what makes a threading lock safe to take from the async endpoint too.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from typing import Any

import httpx

from . import claude_code, config, google_oauth

MAX_ROWS = 200
MAX_FIELD = 300  # characters per table cell
MAX_TEMPLATE = 20_000
MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_RESUME_CHARS = 40_000  # what goes into the prompt; a long resume is ~15k
PREVIEW_CHARS = 300

ROW_FIELDS = ("org", "role", "recruiter_name", "recruiter_email")
RESUME_TYPES = (".pdf", ".docx", ".doc", ".rtf", ".txt", ".md")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ROW_ID = re.compile(r"^[0-9a-f]{32}$")

GMAIL_DRAFTS = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
GMAIL_API_URL = "https://console.cloud.google.com/apis/library/gmail.googleapis.com"
RECONNECT_MESSAGE = "Reconnect Google in Settings to allow Gmail drafts."
CONNECT_MESSAGE = "Connect Google in Settings to save Gmail drafts."
NO_TEXT_MESSAGE = "Couldn't read any text from that file — try a .docx or a text-based PDF."

_lock = threading.Lock()


class OutreachError(Exception):
    """An HTTP status and a user-facing message; main.py turns it into {"detail": ...}."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# -- paths and the state file -------------------------------------------------------------
def _dir() -> Path:
    return config.STATE_DIR / "outreach"


def _state_file() -> Path:
    return _dir() / "state.json"


def _resume_dir() -> Path:
    return _dir() / "resume"


def _resume_text_file() -> Path:
    # Beside the folder, not in it: an uploaded resume.txt would otherwise overwrite it.
    return _dir() / "resume_text.txt"


def _empty() -> dict[str, Any]:
    return {"rows": [], "template": "", "attach_resume": True, "resume": None, "drafts": {}}


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    state = _empty()
    if isinstance(data, dict):
        state.update({k: data[k] for k in state if k in data})
    return state


def _write(state: dict[str, Any]) -> None:
    """Atomic: a crash mid-write leaves the previous file, never half a JSON document."""
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# -- what the UI sees ---------------------------------------------------------------------
def _input_hash(row: dict[str, Any], template: str, resume: dict[str, Any] | None) -> str:
    """What a draft was written from. The recruiter's email isn't in it: it only fills the
    To: line at save time, so changing it doesn't make the text out of date."""
    basis = [row.get("org", ""), row.get("role", ""), row.get("recruiter_name", ""), template,
             (resume or {}).get("sha256", "")]
    return hashlib.sha256(json.dumps(basis).encode("utf-8")).hexdigest()


def _public_draft(draft: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    row = next((r for r in state["rows"] if r["id"] == draft["row_id"]), None)
    stale = bool(
        row is not None
        and draft.get("input_hash")
        and draft["input_hash"] != _input_hash(row, state["template"], state["resume"])
    )
    return {
        "row_id": draft["row_id"],
        "status": draft.get("status", "ready"),
        "subject": draft.get("subject", ""),
        "body": draft.get("body", ""),
        "error": draft.get("error"),
        "gmail_draft_id": draft.get("gmail_draft_id"),
        "saved_at": draft.get("saved_at"),
        "stale": stale,
    }


def _public(state: dict[str, Any]) -> dict[str, Any]:
    resume = state["resume"]
    return {
        "rows": state["rows"],
        "template": state["template"],
        "attach_resume": bool(state["attach_resume"]),
        "resume": None
        if not resume
        else {k: resume[k] for k in ("filename", "chars", "preview", "uploaded_at")},
        "drafts": {rid: _public_draft(d, state) for rid, d in state["drafts"].items()},
    }


def public_state() -> dict[str, Any]:
    with _lock:
        return _public(_load())


# -- the table and the sample email -------------------------------------------------------
def _clean(value: Any, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def update(body: dict[str, Any]) -> dict[str, Any]:
    """Replace the rows, the sample email and the attach toggle (PUT /api/outreach)."""
    rows_in = body.get("rows") or []
    if len(rows_in) > MAX_ROWS:
        raise OutreachError(400, f"That's more than {MAX_ROWS} rows. Split the list into smaller batches.")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for number, raw in enumerate(rows_in, start=1):
        raw = raw if isinstance(raw, dict) else {}
        row = {f: _clean(raw.get(f), MAX_FIELD) for f in ROW_FIELDS}
        email = row["recruiter_email"]
        if email and not _EMAIL.match(email):
            raise OutreachError(400, f"Row {number}: “{email}” doesn't look like an email address.")
        rid = str(raw.get("id") or "").strip().lower()
        if not _ROW_ID.match(rid) or rid in seen:
            rid = uuid.uuid4().hex
        seen.add(rid)
        rows.append({"id": rid, **row})

    template = str(body.get("template") or "").strip()
    if len(template) > MAX_TEMPLATE:
        raise OutreachError(400, "The sample email is too long. Keep it under 20,000 characters.")

    with _lock:
        state = _load()
        state["rows"] = rows
        state["template"] = template
        if body.get("attach_resume") is not None:
            state["attach_resume"] = bool(body["attach_resume"])
        state["drafts"] = {rid: d for rid, d in state["drafts"].items() if rid in seen}
        _write(state)
        return _public(state)


# -- the resume ---------------------------------------------------------------------------
def _safe_filename(name: str, suffix: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(str(name or "")).name)[:100]
    stem = base[: -len(suffix)] if base.lower().endswith(suffix) else base
    return (stem.strip(" ._") or "resume") + suffix


def _pdf_text(data: bytes) -> str:
    from Foundation import NSData
    from Quartz import PDFDocument

    document = PDFDocument.alloc().initWithData_(NSData.dataWithBytes_length_(data, len(data)))
    if document is None:
        raise OutreachError(400, "That file isn't a readable PDF.")
    if document.isLocked():
        raise OutreachError(400, "That PDF is password-protected. Upload an unlocked copy.")
    return str(document.string() or "")


def _textutil_text(data: bytes, suffix: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / f"resume{suffix}"
        source.write_bytes(data)
        try:
            out = subprocess.run(
                ["/usr/bin/textutil", "-convert", "txt", "-stdout", str(source)],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            raise OutreachError(400, "That file couldn't be converted to text.") from None
    if out.returncode != 0:
        raise OutreachError(400, "That file couldn't be converted to text.")
    return out.stdout.decode("utf-8", errors="replace")


def extract_text(data: bytes, suffix: str) -> str:
    if suffix == ".pdf":
        text = _pdf_text(data)
    elif suffix in (".txt", ".md"):
        text = data.decode("utf-8-sig", errors="replace")
    else:
        text = _textutil_text(data, suffix)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(re.sub(r"\s", "", text)) < 20:  # a scanned PDF yields nothing, or a stray page number
        raise OutreachError(400, NO_TEXT_MESSAGE)
    return text


def upload_resume(filename: str, data_base64: str) -> dict[str, Any]:
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix not in RESUME_TYPES:
        raise OutreachError(400, "Upload a PDF, Word (.docx or .doc), RTF or text file.")
    if len(data_base64 or "") > MAX_RESUME_BYTES * 4 // 3 + 8:
        raise OutreachError(400, "That file is over 10 MB.")
    try:
        data = base64.b64decode(data_base64 or "", validate=True)
    except ValueError:
        raise OutreachError(400, "That file couldn't be read.") from None
    if not data:
        raise OutreachError(400, "That file is empty.")
    if len(data) > MAX_RESUME_BYTES:
        raise OutreachError(400, "That file is over 10 MB.")

    text = extract_text(data, suffix)
    stored = _safe_filename(filename, suffix)
    with _lock:
        state = _load()
        folder = _resume_dir()
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.iterdir():  # one resume at a time
            if old.is_file():
                old.unlink()
        (folder / stored).write_bytes(data)
        _resume_text_file().write_text(text, encoding="utf-8")
        for path in (folder / stored, _resume_text_file()):
            path.chmod(0o600)
        state["resume"] = {
            "filename": stored,
            "stored_name": stored,
            "chars": len(text),
            "preview": text[:PREVIEW_CHARS],
            "uploaded_at": _now(),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        _write(state)
        return _public(state)


def delete_resume() -> dict[str, Any]:
    with _lock:
        state = _load()
        folder = _resume_dir()
        if folder.is_dir():
            for old in folder.iterdir():
                if old.is_file():
                    old.unlink()
        _resume_text_file().unlink(missing_ok=True)
        state["resume"] = None
        _write(state)
        return _public(state)


# -- generating ---------------------------------------------------------------------------
def _find_row(state: dict[str, Any], row_id: str) -> dict[str, Any]:
    row = next((r for r in state["rows"] if r["id"] == row_id), None)
    if row is None:
        raise OutreachError(404, "That row no longer exists.")
    return row


async def generate_draft(row_id: str) -> dict[str, Any]:
    """Have Claude Code write this row's email and keep it as the row's draft."""
    with _lock:
        state = _load()
        row = dict(_find_row(state, row_id))
        template = state["template"]
        resume = state["resume"]
        missing = [label for field, label in (("org", "organization"), ("role", "role"),
                                              ("recruiter_name", "recruiter name")) if not row.get(field)]
        if missing:
            raise OutreachError(400, f"Fill in the {', '.join(missing)} for this row first.")
        if not template:
            raise OutreachError(400, "Paste a sample email first.")
        if not resume:
            raise OutreachError(400, "Upload your resume first.")
        try:
            resume_text = _resume_text_file().read_text(encoding="utf-8")[:MAX_RESUME_CHARS]
        except OSError:
            raise OutreachError(400, "Your resume file has gone missing. Upload it again.") from None
        input_hash = _input_hash(row, template, resume)

    try:
        email = await claude_code.generate(row, template, resume_text)
    except claude_code.ClaudeCodeError as exc:
        if exc.kind in ("not_found", "not_signed_in"):
            raise OutreachError(503, exc.message) from None
        with _lock:
            state = _load()
            _find_row(state, row_id)
            previous = state["drafts"].get(row_id, {})
            state["drafts"][row_id] = {**previous, "row_id": row_id, "status": "error", "error": exc.message,
                                       "subject": previous.get("subject", ""), "body": previous.get("body", "")}
            _write(state)
        raise OutreachError(502, exc.message) from None

    with _lock:
        state = _load()
        _find_row(state, row_id)  # deleted while Claude was writing → 404, nothing stored
        previous = state["drafts"].get(row_id, {})
        draft = {
            "row_id": row_id,
            "status": "ready",
            "subject": email["subject"],
            "body": email["body"],
            "error": None,
            # Kept so saving again updates the same Gmail draft instead of adding a second one.
            "gmail_draft_id": previous.get("gmail_draft_id"),
            "saved_at": previous.get("saved_at"),
            "input_hash": input_hash,
        }
        state["drafts"][row_id] = draft
        _write(state)
        return _public_draft(draft, state)


def edit_draft(row_id: str, subject: str, body: str) -> dict[str, Any]:
    with _lock:
        state = _load()
        _find_row(state, row_id)
        draft = state["drafts"].get(row_id)
        if draft is None:
            raise OutreachError(404, "Generate this row's draft first.")
        draft["subject"] = str(subject or "").strip()[:MAX_FIELD * 2]
        draft["body"] = str(body or "").rstrip()[:MAX_TEMPLATE]
        if draft.get("status") in ("saved", "error") and (draft["subject"] or draft["body"]):
            draft["status"] = "ready"  # edited since the last save: it can be saved again
            draft["error"] = None
        _write(state)
        return _public_draft(draft, state)


# -- saving to Gmail ----------------------------------------------------------------------
def build_message(row: dict[str, Any], draft: dict[str, Any], attachment: tuple[str, bytes] | None) -> EmailMessage:
    message = EmailMessage(policy=SMTP)
    email = row.get("recruiter_email", "")
    if email:
        user, _, domain = email.partition("@")
        name = row.get("recruiter_name", "")
        try:
            message["To"] = Address(display_name=name, username=user, domain=domain)
        except (ValueError, IndexError):  # an address the header registry rejects outright
            message["To"] = email
    message["Subject"] = draft.get("subject", "")
    message.set_content(draft.get("body", ""))
    if attachment is not None:
        filename, data = attachment
        kind = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        maintype, _, subtype = kind.partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return message


def encode_raw(message: EmailMessage) -> str:
    """The base64url form Gmail's API takes in message.raw."""
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _gmail_error(response: httpx.Response) -> OutreachError:
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    reasons = {str(e.get("reason", "")) for e in error.get("errors", []) if isinstance(e, dict)}
    reasons |= {str(d.get("reason", "")) for d in error.get("details", []) if isinstance(d, dict)}
    if response.status_code == 401:
        return OutreachError(400, "Google sign-in has expired. Reconnect Google in Settings.")
    if response.status_code == 403:
        if reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
            return OutreachError(
                403, f"Enable the Gmail API for your Google Cloud project: {GMAIL_API_URL}"
            )
        if reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"} or not reasons:
            return OutreachError(403, RECONNECT_MESSAGE)
    return OutreachError(502, f"Gmail didn't accept the draft (HTTP {response.status_code}). Try again.")


def _send_draft(token: str, raw: str, draft_id: str | None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if draft_id:
            response = httpx.put(f"{GMAIL_DRAFTS}/{draft_id}", headers=headers,
                                 json={"id": draft_id, "message": {"raw": raw}}, timeout=30)
            if response.status_code != 404:  # 404: deleted in Gmail since; make a new one
                if response.status_code >= 400:
                    raise _gmail_error(response)
                return response.json()
        response = httpx.post(GMAIL_DRAFTS, headers=headers, json={"message": {"raw": raw}}, timeout=30)
    except httpx.HTTPError:
        raise OutreachError(502, "Couldn't reach Gmail. Check your connection and try again.") from None
    if response.status_code >= 400:
        raise _gmail_error(response)
    return response.json()


def save_draft(row_id: str) -> dict[str, Any]:
    """Create (or update) this row's Gmail draft. Never sends anything."""
    with _lock:
        state = _load()
        row = dict(_find_row(state, row_id))
        draft = dict(state["drafts"].get(row_id) or {})
        if draft.get("status") not in ("ready", "saved"):
            raise OutreachError(400, "Generate this row's draft first.")
        if not (draft.get("subject") and draft.get("body")):
            raise OutreachError(400, "Write a subject and a body before saving.")
        attachment = None
        resume = state["resume"]
        if state["attach_resume"] and resume:
            try:
                attachment = (resume["filename"], (_resume_dir() / resume["stored_name"]).read_bytes())
            except OSError:
                raise OutreachError(400, "Your resume file has gone missing. Upload it again.") from None

    if not google_oauth.is_connected():
        raise OutreachError(400, CONNECT_MESSAGE)
    if not google_oauth.can_draft():
        raise OutreachError(403, RECONNECT_MESSAGE)
    try:
        token = google_oauth.get_valid_access_token()
    except RuntimeError:
        raise OutreachError(400, CONNECT_MESSAGE) from None
    except httpx.HTTPError:
        raise OutreachError(400, "Google sign-in has expired. Reconnect Google in Settings.") from None

    raw = encode_raw(build_message(row, draft, attachment))
    created = _send_draft(token, raw, draft.get("gmail_draft_id"))

    with _lock:
        state = _load()
        _find_row(state, row_id)
        current = state["drafts"].get(row_id) or draft
        # Edited while Gmail was answering: Gmail has the older text, so it isn't "saved" yet.
        unchanged = (current.get("subject"), current.get("body")) == (draft.get("subject"), draft.get("body"))
        current.update({
            "status": "saved" if unchanged else "ready",
            "error": None,
            "gmail_draft_id": created.get("id"),
            "gmail_message_id": (created.get("message") or {}).get("id"),
            "saved_at": _now(),
        })
        state["drafts"][row_id] = current
        _write(state)
        return _public_draft(current, state)


# -- readiness ----------------------------------------------------------------------------
def status() -> dict[str, Any]:
    connected = google_oauth.is_connected()
    can_draft = connected and google_oauth.can_draft()
    message = None if can_draft else (RECONNECT_MESSAGE if connected else CONNECT_MESSAGE)
    return {
        "claude": claude_code.status(),
        "gmail": {"connected": connected, "can_draft": can_draft, "message": message},
    }
