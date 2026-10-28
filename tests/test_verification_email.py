"""fix/send-verification-email: письмо подтверждения адреса действительно уходит.

Контракт:

  * ``EmailService.send_verification_email`` существует и рендерит шаблон
    ``verify_email``. До этой ветки метода не было: signup и resend падали в
    ``except AttributeError``, ответ пользователю был «ок», а письмо не уходило
    вообще ни при каком провайдере;
  * вызов из ``/auth/signup`` и ``/auth/resend-verification`` доходит до
    провайдера (проверяется мок ``EmailService.send``) и несёт тот же токен,
    что лежит в ``users.verification_token``;
  * ссылка несёт ``ROMA_URL_PREFIX`` ровно один раз: базис без префикса
    (``https://asurdev.zo.space``) и базис с уже вписанным ``/roma``
    (``https://asurdev.zo.space/roma``) дают один и тот же путь — двойного
    ``/roma/roma`` нет;
  * токен не попадает ни в консольный вывод провайдера, ни в лог приложения:
    в ``except`` логируется ``exc_info``, а не ``str(e)``, и текст исключения
    (в котором может оказаться verify-ссылка) больше не пишется в лог.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

import db_adapter as db
import deps
import main
import routers.auth as auth_routes
from saas.email.service import EmailProvider, EmailService


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Воркер выключен, реестр и лимитер чистые, префикс не протекает из окружения."""

    async def _noop():
        return None

    monkeypatch.setattr(main, "init_worker", lambda: None)
    monkeypatch.setattr(main, "poll_and_execute", _noop)
    monkeypatch.delenv("ROMA_URL_PREFIX", raising=False)

    original = dict(deps.API_KEYS)
    deps.API_KEYS.clear()
    deps.limiter._storage.reset()
    yield
    deps.limiter._storage.reset()
    deps.API_KEYS.clear()
    deps.API_KEYS.update(original)


@pytest.fixture
def client():
    return TestClient(main.app)


def _capture_send(monkeypatch, svc, sink):
    """Подменить провайдерский вызов ``EmailService.send`` простым сборщиком."""

    def _send(to_email, subject, html_body):
        sink.append({"to": to_email, "subject": subject, "html": html_body})
        return True

    monkeypatch.setattr(svc, "send", _send)
    return sink


def test_send_verification_email_renders_and_calls_provider(monkeypatch):
    """Метод есть, рендерит verify_email и отдаёт письмо провайдеру ровно один раз."""
    svc = EmailService(provider=EmailProvider.CONSOLE)
    sent = _capture_send(monkeypatch, svc, [])

    ok = svc.send_verification_email(
        to_email="e2e@example.invalid",
        tenant_name="E2E Tenant",
        verification_url="https://roma.example.test/auth/verify-email?token=TOK123",
        brand={"app_name": "ROMA", "primary_color": "#6366f1"},
        expiry_hours=24,
    )

    assert ok is True
    assert len(sent) == 1
    mail = sent[0]
    assert mail["to"] == "e2e@example.invalid"
    assert "Verify" in mail["subject"] and "ROMA" in mail["subject"]
    assert 'href="https://roma.example.test/auth/verify-email?token=TOK123"' in mail["html"]
    assert "E2E Tenant" in mail["html"]
    assert "24 hours" in mail["html"]


@pytest.mark.parametrize(
    ("prefix", "base", "expected_link"),
    [
        ("", "https://roma.example.test", "https://roma.example.test/auth/verify-email"),
        ("/roma", "https://asurdev.zo.space", "https://asurdev.zo.space/roma/auth/verify-email"),
        ("/roma", "https://asurdev.zo.space/roma", "https://asurdev.zo.space/roma/auth/verify-email"),
    ],
)
def test_verification_link_carries_mount_prefix_once(monkeypatch, prefix, base, expected_link):
    """Префикс монтирования в ссылке ровно один, токен — только в письме."""
    if prefix:
        monkeypatch.setenv("ROMA_URL_PREFIX", prefix)

    svc = EmailService(provider=EmailProvider.CONSOLE)
    sent = _capture_send(monkeypatch, svc, [])
    svc.send_verification_email(
        to_email="e2e@example.invalid",
        tenant_name="E2E",
        verification_url=f"{base}/auth/verify-email?token=TOK",
        brand={"app_name": "ROMA"},
        expiry_hours=24,
    )

    html = sent[0]["html"]
    assert f'href="{expected_link}?token=TOK"' in html
    assert "/roma/roma" not in html
    # шаблон печатает ссылку дважды: кнопка и «скопируйте ссылку» — токенов тоже два
    assert html.count("token=TOK") == 2


def test_token_not_leaked_to_console_provider_or_log(capsys, caplog):
    """Токен не печатается консольным провайдером и не попадает в лог."""
    token = "TOK" + uuid.uuid4().hex
    svc = EmailService(provider=EmailProvider.CONSOLE)

    with caplog.at_level(logging.DEBUG, logger="roma"):
        assert svc.send_verification_email(
            to_email="e2e@example.invalid",
            tenant_name="E2E",
            verification_url=f"https://roma.example.test/auth/verify-email?token={token}",
            brand={"app_name": "ROMA"},
            expiry_hours=24,
        )

    captured = capsys.readouterr()
    assert "example.invalid" in captured.out, "консольный провайдер должен был отработать"
    assert token not in captured.out + captured.err
    assert token not in caplog.text


class _UserStore:
    """users/tenants в памяти: проверяется проводка signup→провайдер, не SQL."""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.tenants: dict[str, dict] = {}

    def get_user_by_email(self, email: str) -> dict | None:
        user = self.users.get(email)
        return dict(user) if user else None

    def seed_tenants(self, mapping: dict) -> None:
        self.tenants.update(mapping)

    def create_user_with_password(
        self, user_id, email, name, tenant_id, api_key, password_hash, verification_token, token_expires
    ) -> dict:
        self.users[email] = {
            "id": user_id,
            "email": email,
            "name": name,
            "tenant_id": tenant_id,
            "password_hash": password_hash,
            "verification_token": verification_token,
            "verification_token_expires_at": token_expires,
            "email_verified": False,
        }
        return dict(self.users[email])

    def update_verification_token(self, user_id: str, token: str, expires_at) -> bool:
        for user in self.users.values():
            if user["id"] == user_id:
                user["verification_token"] = token
                user["verification_token_expires_at"] = expires_at
                return True
        return False


@pytest.fixture
def user_store(monkeypatch):
    """Подменить users/tenants в памяти: тест не требует PostgreSQL."""
    store = _UserStore()
    monkeypatch.setattr(db, "get_user_by_email", store.get_user_by_email)
    monkeypatch.setattr(db, "seed_tenants", store.seed_tenants)
    monkeypatch.setattr(db, "create_user_with_password", store.create_user_with_password)
    monkeypatch.setattr(db, "update_verification_token", store.update_verification_token)
    monkeypatch.setattr(auth_routes, "register_key", lambda api_key, meta: None)
    monkeypatch.setattr(auth_routes, "BETA_MODE", False)
    monkeypatch.setattr(auth_routes, "BETA_REQUIRE_INVITE", False)
    return store


def test_signup_sends_verification_email(client, user_store, monkeypatch):
    """POST /auth/signup доходит до провайдера; ссылка несёт токен из users."""
    email = f"verify-{uuid.uuid4().hex[:8]}@example.com"
    monkeypatch.setattr(auth_routes, "VERIFICATION_BASE_URL", "https://roma.example.test")
    sent = _capture_send(monkeypatch, auth_routes.email_service, [])

    resp = client.post("/auth/signup", json={"email": email, "password": "correct-horse-1"})

    assert resp.status_code == 201, resp.text
    assert len(sent) == 1, "signup обязан дойти до провайдера"
    mail = sent[0]
    assert mail["to"] == email
    token = user_store.users[email]["verification_token"]
    assert token, "токен сохранён в users"
    assert f"https://roma.example.test/auth/verify-email?token={token}" in mail["html"]
    assert "/roma/roma" not in mail["html"]


def test_resend_sends_verification_email(client, user_store, monkeypatch):
    """POST /auth/resend-verification доходит до провайдера с новым токеном."""
    email = f"verify-{uuid.uuid4().hex[:8]}@example.com"
    monkeypatch.setattr(auth_routes, "VERIFICATION_BASE_URL", "https://asurdev.zo.space")
    sent = _capture_send(monkeypatch, auth_routes.email_service, [])

    signup = client.post("/auth/signup", json={"email": email, "password": "correct-horse-1"})
    assert signup.status_code == 201, signup.text
    first_token = user_store.users[email]["verification_token"]
    sent.clear()

    resp = client.post("/auth/resend-verification", json={"email": email})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "sent"
    assert len(sent) == 1, "resend обязан дойти до провайдера"
    token = user_store.users[email]["verification_token"]
    assert token and token != first_token, "resend выдаёт новый токен"
    assert f"https://asurdev.zo.space/auth/verify-email?token={token}" in sent[0]["html"]


@pytest.fixture
def log_records():
    """Куда пишет подменённый логгер.

    ``logging.disable(CRITICAL)`` в чужих тестах глушит logging на всю сессию,
    а у логгера ``roma`` стоит ``propagate = False`` — caplog его не видит.
    Поэтому тест подменяет сам ``routers.auth.logger``: результат не зависит
    от порядка запуска файлов.
    """
    return []


def test_signup_email_failure_logs_exc_info_without_token(client, user_store, monkeypatch, log_records):
    """Сбой письма не ломает signup и логируется через exc_info, без токена."""
    email = f"verify-{uuid.uuid4().hex[:8]}@example.com"

    def _boom(**_kwargs):
        raise RuntimeError("smtp down")

    class _Recorder:
        def info(self, msg, **kwargs):
            log_records.append(("info", msg, kwargs))

        def warning(self, msg, **kwargs):
            log_records.append(("warning", msg, kwargs))

    monkeypatch.setattr(auth_routes, "logger", _Recorder())
    monkeypatch.setattr(auth_routes.email_service, "send_verification_email", _boom)

    resp = client.post("/auth/signup", json={"email": email, "password": "correct-horse-1"})

    assert resp.status_code == 201, "поведение ответа не меняется"
    warnings = [c for c in log_records if c[0] == "warning" and c[1] == "verification_email_failed"]
    assert warnings, "сбой письма обязан попасть в лог"
    assert warnings[0][2].get("exc_info") is True, "exc_info обязателен: без него теряется причина"
    token = user_store.users[email]["verification_token"]
    assert token and token not in repr(log_records), "verify-токен не должен попадать в лог"
