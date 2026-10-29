"""Admin invite: письмо уходит через EmailService, статус лида — только при успехе.

Контракт ``POST /admin/invite``:

  - ``dry_run`` по умолчанию следует провайдеру (console → dry-run), а не
    наличию SendGrid-ключа;
  - dry-run: письмо не отправляется, лид остаётся ``new``;
  - реальная отправка успешна: ``sent++`` и лид → ``invited``;
  - реальная отправка вернула False: ``failed++``, статус лида не меняется.

Тесты офлайн: ``EmailService.send_invite`` подменяется, db-функции — шпионы.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routers.admin as admin_mod  # noqa: E402
from saas.email.service import EmailService  # noqa: E402

INVITE_CODE = "ROMA-TEST-CODE"


class _Req:
    """Минимальный Request: обработчику нужен только ``json()``."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _call_invite(payload):
    fn = admin_mod.admin_invite
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return asyncio.run(fn(_Req(payload)))


@pytest.fixture
def spy(monkeypatch):
    leads = [
        {"id": 1, "email": "lead1@example.com", "company": "Acme"},
        {"id": 2, "email": "lead2@example.com", "company": "Globex"},
    ]
    logged_sent, logged_failed, statuses, sends = [], [], [], []
    state = {"ok": True}

    def _fake_send(self, to_email, invitation_link, name=None):
        sends.append((to_email, invitation_link, name))
        return state["ok"]

    monkeypatch.setattr(admin_mod.db, "list_leads", lambda status="": [dict(x) for x in leads])
    monkeypatch.setattr(admin_mod.db, "log_email_sent", lambda *a: logged_sent.append(a) or len(logged_sent))
    monkeypatch.setattr(admin_mod.db, "log_email_failed", lambda *a: logged_failed.append(a) or len(logged_failed))
    monkeypatch.setattr(admin_mod.db, "update_lead_status", lambda i, s, notes="": statuses.append((i, s)))
    monkeypatch.setattr(admin_mod, "create_invite", lambda **kw: {"code": INVITE_CODE})
    monkeypatch.setattr(EmailService, "send_invite", _fake_send)

    return {
        "leads": leads,
        "state": state,
        "sends": sends,
        "logged_sent": logged_sent,
        "logged_failed": logged_failed,
        "statuses": statuses,
    }


def test_dry_run_by_default_while_provider_is_console(spy, monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "console")
    monkeypatch.setenv("SENDGRID_API_KEY", "not-empty-but-console-provider")

    out = _call_invite({})

    assert out["dry_run"] is True
    assert spy["sends"] == []


def test_dry_run_sends_nothing_and_keeps_leads_new(spy):
    out = _call_invite({"dry_run": True})

    assert spy["sends"] == []
    assert out["sent"] == 2
    assert out["failed"] == 0
    assert len(spy["logged_sent"]) == 2
    assert spy["statuses"] == []


def test_real_send_success_marks_leads_invited(spy):
    spy["state"]["ok"] = True

    out = _call_invite({"dry_run": False})

    assert len(spy["sends"]) == 2
    assert [s[0] for s in spy["sends"]] == ["lead1@example.com", "lead2@example.com"]
    assert all(f"invite={INVITE_CODE}" in s[1] for s in spy["sends"])
    assert out["sent"] == 2
    assert out["failed"] == 0
    assert out["dry_run"] is False
    assert [s[1] for s in spy["statuses"]] == ["invited", "invited"]


def test_real_send_failure_counts_failed_and_keeps_lead_status(spy):
    spy["state"]["ok"] = False

    out = _call_invite({"dry_run": False})

    assert len(spy["sends"]) == 2
    assert out["sent"] == 0
    assert out["failed"] == 2
    assert len(spy["logged_failed"]) == 2
    assert spy["statuses"] == []
