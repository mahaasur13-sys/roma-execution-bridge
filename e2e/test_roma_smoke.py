"""Первый Playwright smoke ROMA. Маркер e2e. Только чтение (MONEY STOP).

Каталог e2e/ исключён из дефолтного pytest (--ignore=e2e), чтобы канон
``.ci/run-completeness.json`` (collected=1291) не изменился.
Запуск: pytest e2e/ -m e2e --browser=chromium -o addopts=
(-o addopts= снимает --ignore=e2e из pyproject addopts).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_health_ok(roma_base_url: str, playwright) -> None:
    """GET /health → 200, status=ok. PG может быть false (sqlite-режим)."""
    request = playwright.request.new_context(base_url=roma_base_url)
    try:
        resp = request.get("/health")
        assert resp.status == 200
        body = resp.json()
        assert body.get("status") == "ok"
        assert "pg" in body
    finally:
        request.dispose()


def test_metrics_exposes_refund_pending_gauge(roma_base_url: str, playwright) -> None:
    """GET /metrics с loopback без X-Forwarded-For (иначе edge_gate отдаёт 404).

    Имя roma_refund_pending_jobs регистрируется при импорте monitoring.metrics
    (main.py импортирует этот модуль при старте). Значение не проверяем —
    это money-path, только наличие серии.
    """
    request = playwright.request.new_context(base_url=roma_base_url)
    try:
        resp = request.get("/metrics")
        assert resp.status == 200
        text = resp.text()
        assert "roma_refund_pending_jobs" in text
    finally:
        request.dispose()


def test_landing_page_loads(roma_base_url: str, page) -> None:
    """GET / отдаёт static/index.html в настоящем браузере."""
    page.goto(roma_base_url + "/", wait_until="domcontentloaded")
    assert "VIMANA VEGA" in page.title()
    assert page.get_by_role("heading", name="ROMA Execution Bridge").is_visible()
    assert page.get_by_role("link", name="System health").is_visible()


def test_login_form_visible_without_oauth_when_unconfigured(roma_base_url: str, page) -> None:
    """GET /auth/login: поле api_key и Sign In; без CLIENT_ID нет OAuth-кнопок."""
    page.goto(roma_base_url + "/auth/login", wait_until="domcontentloaded")
    assert page.title() == "ROMA — Login"
    assert page.locator('input[name="api_key"]').is_visible()
    assert page.get_by_role("button", name="Sign In").is_visible()
    assert page.locator('a[href*="/auth/oauth/login/"]').count() == 0


def test_dashboard_unauthenticated_redirects_to_login(roma_base_url: str, page) -> None:
    """GET /dashboard без cookie и без X-API-Key → редирект на /auth/login."""
    page.goto(roma_base_url + "/dashboard", wait_until="domcontentloaded")
    assert "/auth/login" in page.url
    assert page.locator('input[name="api_key"]').is_visible()
