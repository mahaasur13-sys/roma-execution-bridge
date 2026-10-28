"""SEC: invitation links carry a single-use invite code, never an API key.

An API key cannot be revoked by the recipient and leaks into mail archives,
referrers and logs, so it is not allowed in a URL. The link points at
``/auth/signup?invite=<code>`` instead, where the code comes from
``create_invite(max_uses=1, note=<email>)``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import auth.invites as invites  # noqa: E402

BASE = "https://roma-execution-bridge-asurdev.zocomputer.io"


def _load_admin():
    import routers.admin as admin_mod

    return admin_mod


def _load_send_invitations():
    spec = importlib.util.spec_from_file_location(
        "send_invitations_under_test", PROJECT_DIR / "scripts" / "send_invitations.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_admin_link_has_no_api_key(monkeypatch):
    admin_mod = _load_admin()
    monkeypatch.setattr(admin_mod, "create_invite", lambda **kw: {"code": "ROMA-AAAA-BBBB"})

    link = admin_mod._invite_link("lead@example.com")

    assert "api_key" not in link
    assert link == (
        f"{BASE}/auth/signup?invite=ROMA-AAAA-BBBB&email=lead%40example.com&utm_source=email&utm_medium=invite"
    )


def test_admin_link_uses_single_use_code(monkeypatch):
    admin_mod = _load_admin()
    seen = {}

    def fake_create_invite(**kwargs):
        seen.update(kwargs)
        return {"code": "ROMA-CCCC-DDDD"}

    monkeypatch.setattr(admin_mod, "create_invite", fake_create_invite)
    link = admin_mod._invite_link("lead@example.com")

    assert seen == {"max_uses": 1, "note": "lead@example.com"}
    assert "invite=ROMA-CCCC-DDDD" in link


def test_admin_link_without_beta_mode(monkeypatch):
    admin_mod = _load_admin()
    monkeypatch.setattr(admin_mod, "create_invite", lambda **kw: {"error": "Beta mode is not enabled"})

    link = admin_mod._invite_link("lead@example.com")

    assert link.startswith(f"{BASE}/auth/signup?")
    assert "invite=" not in link
    assert "api_key" not in link


def test_admin_link_survives_db_error(monkeypatch):
    admin_mod = _load_admin()

    def boom(**kwargs):
        raise RuntimeError("pg down")

    monkeypatch.setattr(admin_mod, "create_invite", boom)

    link = admin_mod._invite_link("lead@example.com")
    assert link.startswith(f"{BASE}/auth/signup?") and "invite=" not in link


def test_send_invitations_link_has_code(monkeypatch):
    module = _load_send_invitations()
    seen = {}

    def fake_create_invite(**kwargs):
        seen.update(kwargs)
        return {"code": "ROMA-EEEE-FFFF"}

    monkeypatch.setattr(invites, "create_invite", fake_create_invite)
    link = module.invite_link("lead@example.com")

    assert seen == {"max_uses": 1, "note": "lead@example.com"}
    assert "invite=ROMA-EEEE-FFFF" in link
    assert "api_key" not in link


def test_email_body_has_no_key():
    module = _load_send_invitations()
    link = f"{BASE}/auth/signup?invite=ROMA-EEEE-FFFF&email=lead%40example.com"

    html, text = module.generate_email_content("Lead", "lead@example.com", link)

    for body in (html, text):
        assert "demo_key" not in body
        assert "демо-ключ" not in body.lower()
        assert "api_key" not in body
        assert "{{" not in body
    assert link in html and link in text


def test_demo_key_env_is_not_read_by_code():
    hits = []
    for path in PROJECT_DIR.rglob("*.py"):
        parts = set(path.parts)
        if ".git" in parts or "__pycache__" in parts or "tests" in parts:
            continue
        if "ROMA_DEMO_API_KEY" in path.read_text(encoding="utf-8", errors="ignore"):
            hits.append(str(path.relative_to(PROJECT_DIR)))
    assert hits == []


def test_create_invite_rejects_beta_off(monkeypatch):
    monkeypatch.setattr(invites, "BETA_MODE", False)
    assert "code" not in invites.create_invite(max_uses=1, note="x@y.z")


# --- signup page -----------------------------------------------------------


def _client():
    from starlette.testclient import TestClient

    import main

    return TestClient(main.app)


def test_signup_page_prefills_and_escapes():
    resp = _client().get(
        "/auth/signup",
        params={"invite": "<script>alert(1)</script>", "email": "<script>alert(2)</script>"},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-store"
    body = resp.text
    assert "alert(1)" not in body.replace("&lt;script&gt;alert(1)&lt;/script&gt;", "")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in body
    assert "name=\"invite_code\"" in body and "name=\"password\"" in body and "name=\"email\"" in body


def test_signup_page_empty_query_is_plain_form():
    resp = _client().get("/auth/signup")

    assert resp.status_code == 200
    assert "{{" not in resp.text


def test_dry_run_does_not_create_invite(monkeypatch):
    module = _load_send_invitations()
    calls = []

    def fake_create_invite(**kwargs):
        calls.append(kwargs)
        return {"code": "ROMA-1111-2222"}

    monkeypatch.setattr(invites, "create_invite", fake_create_invite)

    link = module.invite_link("lead@example.com", dry_run=True)

    assert calls == []
    assert "invite=DRY-RUN" in link
    assert "ROMA-1111-2222" not in link
    assert "api_key" not in link


def test_invite_link_logs_no_email(monkeypatch, caplog):
    admin_mod = _load_admin()
    module = _load_send_invitations()

    def boom(**kwargs):
        raise RuntimeError("pg down")

    monkeypatch.setattr(admin_mod, "create_invite", boom)
    monkeypatch.setattr(invites, "create_invite", boom)

    with caplog.at_level("WARNING"):
        admin_mod._invite_link("lead@example.com")
        module.invite_link("lead@example.com")

    assert caplog.records, "warning was expected"
    for record in caplog.records:
        assert "lead@example.com" not in record.getMessage()
    assert any("RuntimeError" in record.getMessage() for record in caplog.records)
