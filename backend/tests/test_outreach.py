"""Outreach: the recruiter table, resume reading, drafting via Claude Code, and Gmail drafts.

Claude Code is replaced with a fake `claude_code.generate` and Gmail with fake httpx calls, so
nothing here touches the network or runs the real CLI.
"""

from __future__ import annotations

import asyncio
import base64
import email
import json
import stat
import subprocess
import time
from email import policy
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import claude_code, config, google_oauth, outreach

CALENDAR = google_oauth.CALENDAR_SCOPE
COMPOSE = google_oauth.GMAIL_COMPOSE_SCOPE
TEMPLATE = "Subject: Hello\n\nHi [Name], I'd love to talk about [Role] at [Company].\n\nSam"
RESUME = b"Sam Lee\nData engineer. Built ETL pipelines in Python at Initech, 2021-2024.\n"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "state" / "settings.json")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sec")
    for var in ("GOOGLE_ACCESS_TOKEN", "TEMPO_CLAUDE_CLI"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def client():
    from backend.app.main import app

    return TestClient(app)


@pytest.fixture
def fake_claude(monkeypatch):
    """Claude Code that answers instantly; records what it was asked."""
    calls: list = []

    async def generate(row, template, resume_text, **kw):
        calls.append((row, template, resume_text))
        await asyncio.sleep(0)
        return {"subject": f"Hello {row['org']}", "body": f"Hi {row['recruiter_name']},\n\nSam"}

    monkeypatch.setattr(claude_code, "generate", generate)
    return calls


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _row(**kw):
    row = {"org": "Acme", "role": "Data Engineer", "recruiter_name": "Ana Pérez", "recruiter_email": "ana@acme.com"}
    row.update(kw)
    return row


def _setup(client, rows=None, template=TEMPLATE, attach=True, resume=RESUME, filename="resume.txt"):
    state = client.put("/api/outreach", json={"rows": rows or [_row()], "template": template,
                                              "attach_resume": attach}).json()
    if resume is not None:
        r = client.post("/api/outreach/resume", json={"filename": filename, "data_base64": _b64(resume)})
        assert r.status_code == 200, r.text
        state = r.json()
    return state


def _connect(scopes=f"{CALENDAR} {COMPOSE}"):
    settings = {"google_access_token": "at", "google_token_expiry": time.time() + 3600,
                "google_refresh_token": "rt"}
    if scopes:
        settings["google_scopes"] = scopes
    config.save_settings(settings)


def _pdf(text: str | None) -> bytes:
    """A minimal one-page PDF, optionally with a line of Helvetica text, with a correct xref."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


# -- the table ----------------------------------------------------------------------------
def test_state_round_trip_assigns_ids(client):
    state = client.put("/api/outreach", json={
        "rows": [_row(org="  Acme  "), _row(org="Globex", recruiter_email="")],
        "template": f"  {TEMPLATE}  ",
        "attach_resume": False,
    }).json()

    assert [r["org"] for r in state["rows"]] == ["Acme", "Globex"]
    assert all(len(r["id"]) == 32 for r in state["rows"])
    assert state["rows"][0]["id"] != state["rows"][1]["id"]
    assert state["template"] == TEMPLATE
    assert state["attach_resume"] is False and state["resume"] is None and state["drafts"] == {}
    assert client.get("/api/outreach").json() == state


def test_fresh_state(client):
    assert client.get("/api/outreach").json() == {
        "rows": [], "template": "", "attach_resume": True, "resume": None, "drafts": {},
    }


def test_existing_ids_kept_duplicates_replaced(client):
    rid = "a" * 32
    rows = client.put("/api/outreach", json={"rows": [_row(id=rid), _row(id=rid), _row(id="bogus")]}).json()["rows"]
    assert rows[0]["id"] == rid
    assert len({r["id"] for r in rows}) == 3


def test_bad_email_is_rejected_with_row_number(client):
    r = client.put("/api/outreach", json={"rows": [_row(), _row(recruiter_email="ana at acme")]})
    assert r.status_code == 400
    assert "Row 2" in r.json()["detail"] and "ana at acme" in r.json()["detail"]


def test_too_many_rows(client):
    r = client.put("/api/outreach", json={"rows": [_row()] * (outreach.MAX_ROWS + 1)})
    assert r.status_code == 400


def test_state_file_is_private(client):
    client.put("/api/outreach", json={"rows": [_row()]})
    mode = (config.STATE_DIR / "outreach" / "state.json").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


# -- the resume ---------------------------------------------------------------------------
def test_text_resume(client):
    state = _setup(client)
    resume = state["resume"]
    assert resume["filename"] == "resume.txt"
    assert resume["chars"] == len(RESUME.decode().strip())
    assert resume["preview"].startswith("Sam Lee")
    assert resume["uploaded_at"].endswith("Z")
    assert set(resume) == {"filename", "chars", "preview", "uploaded_at"}
    # The original is kept for attaching; the extracted text doesn't overwrite it.
    assert (config.STATE_DIR / "outreach" / "resume" / "resume.txt").read_bytes() == RESUME


def test_pdf_resume_via_pdfkit(client):
    state = _setup(client, resume=_pdf("Sam Lee - Data Engineer at Initech since 2021"), filename="CV 2026.pdf")
    assert state["resume"]["filename"] == "CV 2026.pdf"
    assert "Data Engineer at Initech" in state["resume"]["preview"]


def test_docx_resume_via_textutil(client, tmp_path):
    source = tmp_path / "cv.txt"
    source.write_bytes(RESUME)
    subprocess.run(["/usr/bin/textutil", "-convert", "docx", str(source), "-output", str(tmp_path / "cv.docx")],
                   check=True)
    state = _setup(client, resume=(tmp_path / "cv.docx").read_bytes(), filename="cv.docx")
    assert "ETL pipelines" in state["resume"]["preview"]


@pytest.mark.parametrize(
    "filename, data, fragment",
    [
        ("scan.pdf", _pdf(None), "Couldn't read any text"),
        ("broken.pdf", b"%PDF-1.4 nonsense", "readable PDF"),
        ("photo.png", b"\x89PNG", "Upload a PDF"),
        ("empty.txt", b"", "empty"),
        ("short.txt", b"Sam", "Couldn't read any text"),
    ],
)
def test_unusable_resumes(client, filename, data, fragment):
    r = client.post("/api/outreach/resume", json={"filename": filename, "data_base64": _b64(data)})
    assert r.status_code == 400 and fragment in r.json()["detail"]


def test_resume_size_limit_and_bad_base64(client, monkeypatch):
    monkeypatch.setattr(outreach, "MAX_RESUME_BYTES", 100)
    r = client.post("/api/outreach/resume", json={"filename": "cv.txt", "data_base64": _b64(b"x" * 200)})
    assert r.status_code == 400 and "10 MB" in r.json()["detail"]
    r = client.post("/api/outreach/resume", json={"filename": "cv.txt", "data_base64": "not base64!!"})
    assert r.status_code == 400


def test_new_resume_replaces_old_and_delete_clears(client):
    _setup(client, filename="first.txt")
    state = _setup(client, filename="second.md", resume=b"# Sam Lee\n\nAnalytics engineer, dbt and Python.")
    folder = config.STATE_DIR / "outreach" / "resume"
    assert state["resume"]["filename"] == "second.md"
    assert [p.name for p in folder.iterdir()] == ["second.md"]

    state = client.delete("/api/outreach/resume").json()
    assert state["resume"] is None
    assert list(folder.iterdir()) == []
    assert not (config.STATE_DIR / "outreach" / "resume_text.txt").exists()


def test_unsafe_filename_is_cleaned(client):
    state = _setup(client, filename="../../etc/pass wd?.txt")
    assert state["resume"]["filename"] == "pass wd.txt"


# -- generating ---------------------------------------------------------------------------
def test_generate_happy_path(client, fake_claude):
    state = _setup(client)
    rid = state["rows"][0]["id"]

    r = client.post(f"/api/outreach/generate/{rid}")

    assert r.status_code == 200, r.text
    assert r.json() == {"row_id": rid, "status": "ready", "subject": "Hello Acme",
                        "body": "Hi Ana Pérez,\n\nSam", "error": None, "gmail_draft_id": None,
                        "saved_at": None, "stale": False}
    row, template, resume_text = fake_claude[0]
    assert row["org"] == "Acme" and template == TEMPLATE and "ETL pipelines" in resume_text
    assert client.get("/api/outreach").json()["drafts"][rid]["status"] == "ready"


@pytest.mark.parametrize(
    "rows, template, resume, fragment",
    [
        ([_row(recruiter_name="")], TEMPLATE, RESUME, "contact name"),
        ([_row(org="", recruiter_name="")], TEMPLATE, RESUME, "org name, contact name"),
        ([_row()], "", RESUME, "sample email"),
        ([_row()], TEMPLATE, None, "resume"),
    ],
)
def test_generate_needs_inputs(client, fake_claude, rows, template, resume, fragment):
    """Org and contact name are required; role is optional (the sample email can carry it)."""
    state = _setup(client, rows=rows, template=template, resume=resume)
    r = client.post(f"/api/outreach/generate/{state['rows'][0]['id']}")
    assert r.status_code == 400 and fragment in r.json()["detail"]
    assert fake_claude == []


def test_generate_unknown_row(client, fake_claude):
    _setup(client)
    assert client.post(f"/api/outreach/generate/{'f' * 32}").status_code == 404


@pytest.mark.parametrize("kind", ["not_found", "not_signed_in"])
def test_generate_when_claude_code_unavailable(client, monkeypatch, kind):
    message = {"not_found": claude_code.NOT_FOUND_MESSAGE, "not_signed_in": claude_code.NOT_SIGNED_IN_MESSAGE}[kind]

    async def generate(*a, **k):
        raise claude_code.ClaudeCodeError(kind, message)

    monkeypatch.setattr(claude_code, "generate", generate)
    rid = _setup(client)["rows"][0]["id"]
    r = client.post(f"/api/outreach/generate/{rid}")
    assert r.status_code == 503 and r.json()["detail"] == message
    assert client.get("/api/outreach").json()["drafts"] == {}  # nothing to show for it


def test_generate_failure_is_stored_as_error(client, monkeypatch):
    async def generate(*a, **k):
        raise claude_code.ClaudeCodeError("failed", "Claude Code took too long to answer. Try again.")

    monkeypatch.setattr(claude_code, "generate", generate)
    rid = _setup(client)["rows"][0]["id"]
    r = client.post(f"/api/outreach/generate/{rid}")
    assert r.status_code == 502 and "too long" in r.json()["detail"]
    draft = client.get("/api/outreach").json()["drafts"][rid]
    assert draft["status"] == "error" and "too long" in draft["error"]


def test_generate_through_the_sdk(client, monkeypatch, tmp_path):
    """The real claude_code.generate, with only the SDK's query faked."""
    import claude_agent_sdk as sdk

    cli = tmp_path / "bin" / "claude"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\necho '2.1.0 (Claude Code)'\n")
    cli.chmod(0o755)
    monkeypatch.setenv("TEMPO_CLAUDE_CLI", str(cli))

    async def query(*, prompt, options=None, transport=None):
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=2, session_id="s",
                                structured_output={"subject": "Data Engineer at Acme", "body": "Hi Ana,"})

    monkeypatch.setattr(sdk, "query", query)
    rid = _setup(client)["rows"][0]["id"]
    draft = client.post(f"/api/outreach/generate/{rid}").json()
    assert draft["subject"] == "Data Engineer at Acme" and draft["status"] == "ready"


def test_concurrent_generations_all_persist(monkeypatch):
    async def generate(row, template, resume_text, **kw):
        await asyncio.sleep(0.02)  # interleave the three
        return {"subject": row["org"], "body": "x"}

    monkeypatch.setattr(claude_code, "generate", generate)
    rows = outreach.update({"rows": [_row(org=o) for o in ("A", "B", "C")], "template": TEMPLATE})["rows"]
    outreach.upload_resume("cv.txt", _b64(RESUME))

    async def all_three():
        return await asyncio.gather(*(outreach.generate_draft(r["id"]) for r in rows))

    results = asyncio.run(all_three())
    assert sorted(d["subject"] for d in results) == ["A", "B", "C"]
    drafts = outreach.public_state()["drafts"]
    assert {drafts[r["id"]]["subject"] for r in rows} == {"A", "B", "C"}


def test_removed_row_drops_its_draft(client, fake_claude):
    state = _setup(client, rows=[_row(org="A"), _row(org="B")])
    a, b = state["rows"]
    for row in (a, b):
        client.post(f"/api/outreach/generate/{row['id']}")
    state = client.put("/api/outreach", json={"rows": [a], "template": TEMPLATE}).json()
    assert list(state["drafts"]) == [a["id"]]


def test_stale_when_inputs_change(client, fake_claude):
    state = _setup(client)
    row = state["rows"][0]
    client.post(f"/api/outreach/generate/{row['id']}")

    def stale():
        return client.get("/api/outreach").json()["drafts"][row["id"]]["stale"]

    assert stale() is False
    client.put("/api/outreach", json={"rows": [{**row, "recruiter_email": "a.perez@acme.com"}], "template": TEMPLATE})
    assert stale() is False  # only fills To:, the text is still right
    client.put("/api/outreach", json={"rows": [{**row, "role": "Analytics Engineer"}], "template": TEMPLATE})
    assert stale() is True
    client.put("/api/outreach", json={"rows": [row], "template": TEMPLATE})
    assert stale() is False
    client.post("/api/outreach/resume", json={"filename": "cv.txt", "data_base64": _b64(RESUME + b"Also SQL.")})
    assert stale() is True


# -- editing ------------------------------------------------------------------------------
def test_edit_draft(client, fake_claude):
    rid = _setup(client)["rows"][0]["id"]
    assert client.put(f"/api/outreach/drafts/{rid}", json={"subject": "s", "body": "b"}).status_code == 404
    client.post(f"/api/outreach/generate/{rid}")

    draft = client.put(f"/api/outreach/drafts/{rid}", json={"subject": " New ", "body": "Body\n\n"}).json()
    assert draft["subject"] == "New" and draft["body"] == "Body" and draft["status"] == "ready"


# -- the email itself ---------------------------------------------------------------------
def _parse(raw: str) -> email.message.EmailMessage:
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


def test_message_with_recipient_and_attachment():
    msg = outreach.build_message(_row(), {"subject": "Café chat — Data Engineer", "body": "Hi Ana,\n\nSam"},
                                 ("resume.pdf", b"%PDF-1.4 x"))
    parsed = _parse(outreach.encode_raw(msg))
    assert parsed["To"] == "Ana Pérez <ana@acme.com>"
    assert parsed["Subject"] == "Café chat — Data Engineer"
    # SMTP policy: CRLF line endings, as RFC 5322 (and Gmail's raw upload) expects.
    assert parsed.get_body(("plain",)).get_content().strip() == "Hi Ana,\r\n\r\nSam"
    attachments = list(parsed.iter_attachments())
    assert [(a.get_filename(), a.get_content_type()) for a in attachments] == [("resume.pdf", "application/pdf")]
    assert attachments[0].get_content() == b"%PDF-1.4 x"


def test_message_without_recipient_or_attachment():
    msg = outreach.build_message(_row(recruiter_email=""), {"subject": "S", "body": "B"}, None)
    parsed = _parse(outreach.encode_raw(msg))
    assert parsed["To"] is None
    assert not parsed.is_multipart()


# -- saving to Gmail ----------------------------------------------------------------------
class _Resp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.fixture
def gmail(monkeypatch):
    """Fake Gmail drafts API. Set `.put_status` / `.post_response` to steer it."""

    class Gmail:
        calls: list = []
        put_status = 200
        post_response = _Resp(200, {"id": "r-new", "message": {"id": "m-new"}})

        def post(self, url, headers=None, json=None, timeout=None):
            self.calls.append(("POST", url, json))
            return self.post_response

        def put(self, url, headers=None, json=None, timeout=None):
            self.calls.append(("PUT", url, json))
            return _Resp(self.put_status, {"id": json["id"], "message": {"id": "m-upd"}})

    fake = Gmail()
    fake.calls = []
    monkeypatch.setattr(outreach.httpx, "post", fake.post)
    monkeypatch.setattr(outreach.httpx, "put", fake.put)
    return fake


def _generated(client, **kw):
    state = _setup(client, **kw)
    rid = state["rows"][0]["id"]
    assert client.post(f"/api/outreach/generate/{rid}").status_code == 200
    return rid


def test_save_creates_gmail_draft(client, fake_claude, gmail):
    _connect()
    rid = _generated(client)

    draft = client.post(f"/api/outreach/drafts/{rid}/save").json()

    assert draft["status"] == "saved" and draft["gmail_draft_id"] == "r-new" and draft["saved_at"]
    method, url, payload = gmail.calls[0]
    assert (method, url) == ("POST", outreach.GMAIL_DRAFTS)
    parsed = _parse(payload["message"]["raw"])
    assert parsed["To"] == "Ana Pérez <ana@acme.com>" and parsed["Subject"] == "Hello Acme"
    assert [a.get_filename() for a in parsed.iter_attachments()] == ["resume.txt"]


def test_save_without_attachment_when_toggled_off(client, fake_claude, gmail):
    _connect()
    rid = _generated(client, attach=False)
    client.post(f"/api/outreach/drafts/{rid}/save")
    assert list(_parse(gmail.calls[0][2]["message"]["raw"]).iter_attachments()) == []


def test_save_again_updates_the_same_draft(client, fake_claude, gmail):
    _connect()
    rid = _generated(client)
    client.post(f"/api/outreach/drafts/{rid}/save")
    edited = client.put(f"/api/outreach/drafts/{rid}", json={"subject": "Edited", "body": "New body"}).json()
    assert edited["status"] == "ready" and edited["gmail_draft_id"] == "r-new"

    draft = client.post(f"/api/outreach/drafts/{rid}/save").json()

    method, url, payload = gmail.calls[1]
    assert (method, url) == ("PUT", f"{outreach.GMAIL_DRAFTS}/r-new") and payload["id"] == "r-new"
    assert _parse(payload["message"]["raw"])["Subject"] == "Edited"
    assert draft["status"] == "saved" and draft["gmail_draft_id"] == "r-new"


def test_regenerating_keeps_the_gmail_draft_id(client, fake_claude, gmail):
    _connect()
    rid = _generated(client)
    client.post(f"/api/outreach/drafts/{rid}/save")
    draft = client.post(f"/api/outreach/generate/{rid}").json()
    assert draft["status"] == "ready" and draft["gmail_draft_id"] == "r-new"


def test_save_recreates_a_draft_deleted_in_gmail(client, fake_claude, gmail):
    _connect()
    rid = _generated(client)
    client.post(f"/api/outreach/drafts/{rid}/save")
    gmail.put_status = 404
    gmail.post_response = _Resp(200, {"id": "r-2", "message": {"id": "m-2"}})

    draft = client.post(f"/api/outreach/drafts/{rid}/save").json()

    assert [c[0] for c in gmail.calls] == ["POST", "PUT", "POST"]
    assert draft["gmail_draft_id"] == "r-2"


def test_save_before_generating(client, gmail):
    _connect()
    rid = _setup(client)["rows"][0]["id"]
    r = client.post(f"/api/outreach/drafts/{rid}/save")
    assert r.status_code == 400 and "Generate" in r.json()["detail"]


@pytest.mark.parametrize(
    "scopes, status, detail",
    [
        (None, 403, outreach.RECONNECT_MESSAGE),  # connected before Outreach existed
        (CALENDAR, 403, outreach.RECONNECT_MESSAGE),  # unticked Gmail on the consent screen
    ],
)
def test_save_needs_gmail_scope(client, fake_claude, gmail, scopes, status, detail):
    _connect(scopes=scopes)
    rid = _generated(client)
    r = client.post(f"/api/outreach/drafts/{rid}/save")
    assert r.status_code == status and r.json()["detail"] == detail
    assert gmail.calls == []


def test_save_when_not_connected(client, fake_claude, gmail):
    rid = _generated(client)
    r = client.post(f"/api/outreach/drafts/{rid}/save")
    assert r.status_code == 400 and r.json()["detail"] == outreach.CONNECT_MESSAGE


@pytest.mark.parametrize(
    "response, status, fragment",
    [
        (_Resp(403, {"error": {"code": 403, "errors": [{"reason": "accessNotConfigured"}],
                               "details": [{"reason": "SERVICE_DISABLED"}]}}), 403, "Enable the Gmail API"),
        (_Resp(403, {"error": {"code": 403, "errors": [{"reason": "insufficientPermissions"}]}}), 403, "Reconnect"),
        (_Resp(401, {"error": {"code": 401}}), 400, "expired"),
        (_Resp(500, {"error": {"code": 500}}), 502, "HTTP 500"),
    ],
)
def test_gmail_errors(client, fake_claude, gmail, response, status, fragment):
    _connect()
    rid = _generated(client)
    gmail.post_response = response
    r = client.post(f"/api/outreach/drafts/{rid}/save")
    assert r.status_code == status and fragment in r.json()["detail"]
    assert client.get("/api/outreach").json()["drafts"][rid]["status"] == "ready"


def test_gmail_unreachable(client, fake_claude, monkeypatch):
    _connect()
    rid = _generated(client)

    def offline(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(outreach.httpx, "post", offline)
    r = client.post(f"/api/outreach/drafts/{rid}/save")
    assert r.status_code == 502 and "reach Gmail" in r.json()["detail"]


# -- readiness and the Google scope -------------------------------------------------------
def test_status(client, tmp_path, monkeypatch):
    cli = tmp_path / "bin" / "claude"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\necho '2.1.0 (Claude Code)'\n")
    cli.chmod(0o755)
    monkeypatch.setenv("TEMPO_CLAUDE_CLI", str(cli))
    claude_code._version_cache.clear()

    body = client.get("/api/outreach/status").json()
    assert body["claude"] == {"available": True, "path": str(cli), "version": "2.1.0 (Claude Code)", "message": None}
    assert body["gmail"] == {"connected": False, "can_draft": False, "message": outreach.CONNECT_MESSAGE}

    _connect(scopes=None)
    assert client.get("/api/outreach/status").json()["gmail"] == {
        "connected": True, "can_draft": False, "message": outreach.RECONNECT_MESSAGE}

    _connect()
    assert client.get("/api/outreach/status").json()["gmail"] == {
        "connected": True, "can_draft": True, "message": None}


def test_settings_tell_the_sidebar_about_gmail_drafts(client):
    """The sidebar is the only Google control, so it needs to know when to offer Reconnect."""
    assert client.get("/api/settings").json()["google_can_draft"] is False
    _connect(scopes=None)  # a sign-in from before Outreach: Calendar only
    assert client.get("/api/settings").json()["google_can_draft"] is False
    _connect()
    assert client.get("/api/settings").json()["google_can_draft"] is True
    assert client.post("/api/google/disconnect").json()["google_can_draft"] is False


def test_auth_url_asks_for_gmail_compose():
    assert "gmail.compose" in google_oauth.build_auth_url("st") and "calendar.events" in google_oauth.build_auth_url("st")


def test_granted_scopes_are_recorded_and_forgotten():
    google_oauth.store_tokens({"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
                               "scope": f"{CALENDAR} {COMPOSE}"})
    assert google_oauth.can_draft() is True
    assert json.loads(config.SETTINGS_FILE.read_text())["google_scopes"] == f"{CALENDAR} {COMPOSE}"

    google_oauth.store_tokens({"access_token": "at2", "expires_in": 3600})  # a refresh without scope
    assert google_oauth.can_draft() is True

    google_oauth.disconnect()
    assert google_oauth.can_draft() is False
    assert "google_scopes" not in json.loads(config.SETTINGS_FILE.read_text())


# -- CSV import ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        # no header: the four columns in order; quoted commas; a missing trailing email
        ('Acme,Data Engineer,Ana,ana@acme.com\nGlobex,"Backend, Platform",Raj,',
         [("Acme", "Data Engineer", "Ana", "ana@acme.com"), ("Globex", "Backend, Platform", "Raj", "")]),
        # cells copied out of a spreadsheet arrive tab-separated, with CRLF and blank lines
        ("Acme\tData Engineer\tAna\tana@acme.com\r\n\r\nGlobex\tSRE\tRaj\traj@globex.com\r\n",
         [("Acme", "Data Engineer", "Ana", "ana@acme.com"), ("Globex", "SRE", "Raj", "raj@globex.com")]),
        # a header picks columns by name, in any order, and ignores the ones it doesn't know
        ("Email,Company,Notes,Recruiter Name,Job Title\nana@acme.com,Acme,met at fair,Ana,DE",
         [("Acme", "DE", "Ana", "ana@acme.com")]),
        # European-style semicolons
        ("org;role;recruiter;email\nAcme;DE;Ana;ana@acme.com", [("Acme", "DE", "Ana", "ana@acme.com")]),
    ],
)
def test_parse_csv(text, expected):
    rows = outreach.parse_csv(text)
    assert [(r["org"], r["role"], r["recruiter_name"], r["recruiter_email"]) for r in rows] == expected


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("", "Paste at least one row"),
        ("  \n\n", "Paste at least one row"),
        ("org,role,recruiter,email\n", "no rows under it"),
        ("ana@acme.com,https://acme.com", "Line 1 has no org name"),
        ("Acme,DE,Ana,ana@acme", "doesn't look like an email"),
    ],
)
def test_parse_csv_rejects(text, fragment):
    with pytest.raises(outreach.OutreachError) as err:
        outreach.parse_csv(text)
    assert err.value.status == 400 and fragment in err.value.detail


def test_import_replaces_rows_and_keeps_matching_drafts(client, fake_claude):
    state = _setup(client, rows=[_row(org="Acme", recruiter_name="Ana"), _row(org="Initech", recruiter_name="Bo")])
    acme_id = state["rows"][0]["id"]
    assert client.post(f"/api/outreach/generate/{acme_id}").status_code == 200

    r = client.post("/api/outreach/import", json={
        "csv": "org,role,recruiter name,email\nacme,data engineer,ana,new@acme.com\nGlobex,SRE,Raj,"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [row["org"] for row in body["rows"]] == ["acme", "Globex"]  # Initech is gone
    assert body["rows"][0]["id"] == acme_id  # same org/role/recruiter → same row
    assert body["rows"][0]["recruiter_email"] == "new@acme.com"
    assert list(body["drafts"]) == [acme_id]
    assert body["template"] == TEMPLATE  # the sample email is untouched


def test_import_reports_bad_rows(client):
    _setup(client)
    r = client.post("/api/outreach/import", json={"csv": "org,role,contact,email\nAcme,DE,Ana,not-an-email"})
    assert r.status_code == 400 and "doesn't look like an email" in r.json()["detail"]
    r = client.post("/api/outreach/import", json={"csv": ""})
    assert r.status_code == 400


# The user's real sheet: "Org - blurb (city)", an empty column, the website, the contact.
SHEET = [
    ("Superluminal Medicines Inc. - AI-driven GPCR drug design (Boston)", "", "https://www.superluminalrx.com/", "Georgia McGaughey"),
    ("Aureka Biotechnologies, Inc. - AI antibody discovery (Laguna Hills)", "", "https://www.aurekabio.com/", "Weian Zhao"),
    ("Manifold Bio - AI protein engineering platform (Boston)", "", "https://www.manifold.bio/", "Gleb Kuznetsov"),
]
SHEET_ROWS = [
    ("Superluminal Medicines Inc.", "Georgia McGaughey", "AI-driven GPCR drug design (Boston)", "https://www.superluminalrx.com/"),
    ("Aureka Biotechnologies, Inc.", "Weian Zhao", "AI antibody discovery (Laguna Hills)", "https://www.aurekabio.com/"),
    ("Manifold Bio", "Gleb Kuznetsov", "AI protein engineering platform (Boston)", "https://www.manifold.bio/"),
]
_shape = lambda rows: [(r["org"], r["recruiter_name"], r["about"], r["website"]) for r in rows]


@pytest.mark.parametrize("sep", ["\t", "        "], ids=["copied from Excel (tabs)", "tabs turned into spaces"])
def test_parse_cells_copied_from_a_sheet(sep):
    rows = outreach.parse_csv("\n".join(sep.join(cells) for cells in SHEET))
    assert _shape(rows) == SHEET_ROWS
    assert all(r["role"] == "" and r["recruiter_email"] == "" for r in rows)


def test_parse_header_with_about_and_website():
    rows = outreach.parse_csv("Company,Website,Description,Founder,Email\nManifold Bio,manifold.bio,Protein design,Gleb,gleb@manifold.bio")
    assert rows == [{"org": "Manifold Bio", "role": "", "recruiter_name": "Gleb", "recruiter_email": "gleb@manifold.bio",
                     "about": "Protein design", "website": "manifold.bio"}]


def _xlsx(records) -> bytes:
    import io as _io

    import openpyxl

    book = openpyxl.Workbook()
    for rec in records:
        book.active.append(list(rec))
    out = _io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_import_xlsx_file(client):
    _setup(client)
    r = client.post("/api/outreach/import-file", json={"filename": "Leads.XLSX", "data_base64": _b64(_xlsx(SHEET))})
    assert r.status_code == 200, r.text
    assert _shape(r.json()["rows"]) == SHEET_ROWS


def test_import_xlsx_with_header_and_numbers(client):
    _setup(client)
    sheet = [("Org", "Role", "Contact", "Email", "Batch"), ("Acme", "SRE", "Ana", "ana@acme.com", 2024.0)]
    rows = client.post("/api/outreach/import-file", json={"filename": "a.xlsx", "data_base64": _b64(_xlsx(sheet))}).json()["rows"]
    assert [(r["org"], r["role"], r["recruiter_name"], r["recruiter_email"]) for r in rows] == [("Acme", "SRE", "Ana", "ana@acme.com")]


def test_import_csv_file_with_bom(client):
    _setup(client)
    text = "﻿org,role,recruiter name,email\nAcme,SRE,Ana,ana@acme.com\n"
    r = client.post("/api/outreach/import-file", json={"filename": "leads.csv", "data_base64": _b64(text.encode("utf-8"))})
    assert r.status_code == 200, r.text
    assert r.json()["rows"][0]["org"] == "Acme"  # the BOM didn't stick to the header


@pytest.mark.parametrize(
    "filename, data, fragment",
    [
        ("leads.numbers", b"x", "In Numbers: File"),
        ("leads.xlsx", b"not a zip", "Couldn't read that spreadsheet"),
        ("leads.xls", b"not an xls", "Couldn't read that spreadsheet"),
    ],
)
def test_import_file_rejects(client, filename, data, fragment):
    _setup(client)
    r = client.post("/api/outreach/import-file", json={"filename": filename, "data_base64": _b64(data)})
    assert r.status_code == 400 and fragment in r.json()["detail"]


def test_parse_endpoint_returns_rows_without_saving(client):
    _setup(client)
    before = client.get("/api/outreach").json()["rows"]
    r = client.post("/api/outreach/parse", json={"csv": "\n".join("\t".join(c) for c in SHEET)})
    assert r.status_code == 200, r.text
    assert _shape(r.json()["rows"]) == SHEET_ROWS
    assert all("id" not in row for row in r.json()["rows"])
    assert client.get("/api/outreach").json()["rows"] == before  # nothing saved
    assert client.post("/api/outreach/parse", json={"csv": "  "}).status_code == 400


# From the user's Google Sheet: contact (D) comes before role (E), and a cell can name two people.
SHEET_WITH_ROLES = (
    "Chai Discovery - AI antibody design (San Francisco)\t\thttps://www.chaidiscovery.com/\tAvi Asherov, Joshua Meier\tSoftware Engineer, Product\n"
    "Aureka Biotechnologies, Inc. - AI antibody discovery (Laguna Hills)\t\thttps://www.aurekabio.com/\tWeian Zhao\t\tcontact@aurekabio.com\n"
    "Manifold Bio - AI protein engineering platform (Boston)\t\thttps://www.manifold.bio/\tGleb Kuznetsov\tResearch Engineer\n"
    "Manas AI - neuro-symbolic AI drug discovery (San Francisco)\n"
)


def test_parse_tells_roles_from_names_in_any_order():
    rows = outreach.parse_csv(SHEET_WITH_ROLES)
    assert [(r["org"], r["role"], r["recruiter_name"], r["recruiter_email"]) for r in rows] == [
        ("Chai Discovery", "Software Engineer, Product", "Avi Asherov, Joshua Meier", ""),
        ("Aureka Biotechnologies, Inc.", "", "Weian Zhao", "contact@aurekabio.com"),
        ("Manifold Bio", "Research Engineer", "Gleb Kuznetsov", ""),
        ("Manas AI", "", "", ""),  # org only: fine to paste, contact filled in later
    ]
    assert rows[3]["about"] == "neuro-symbolic AI drug discovery (San Francisco)"


@pytest.mark.parametrize(
    "line, role, contact",
    [
        ("Acme,Data Engineer,Ana,ana@acme.com", "Data Engineer", "Ana"),
        ("Globex,SRE,Raj", "SRE", "Raj"),
        ("Acme,DE,Ana", "DE", "Ana"),  # unknown acronym before the contact: still the role
        ("Initech,Bo Chen", "", "Bo Chen"),
        ("Initech,ML Intern,Jean-Luc O'Neil & J. Smith", "ML Intern", "Jean-Luc O'Neil & J. Smith"),
        ("Initech,Jean-Luc O'Neil,Research Scientist", "Research Scientist", "Jean-Luc O'Neil"),
    ],
)
def test_parse_role_and_contact(line, role, contact):
    row = outreach.parse_csv(line)[0]
    assert (row["role"], row["recruiter_name"]) == (role, contact)
