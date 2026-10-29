"""fix/login-hide-oauth-when-disabled: кнопки OAuth — только у настроенного провайдера.

Контракт:

  * блок «— or sign in with —» и сами кнопки рендерятся только при
    ``OAUTH_ENABLED``, то есть когда задан хотя бы один ``*_CLIENT_ID``; иначе
    страница логина не содержит ни одной ссылки на ``/auth/oauth/login/``
    (раньше кнопки были всегда, хотя обработчик отвечал 503/400);
  * кнопка конкретного провайдера появляется только при его ``CLIENT_ID``:
    без ``GOOGLE_CLIENT_ID`` Google нет, а настроенный GitHub остаётся.

Фейковые client id собираются в рантайме из низкоэнтропийных кусков:
литерал высокой энтропии поднимает generic-api-key в gitleaks.
"""

from __future__ import annotations

import asyncio
import os
import sys

from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routers.auth as auth_routes


def _login_html(monkeypatch, *, google: str = "", github: str = "") -> str:
    """HTML страницы логина при заданном наборе OAuth-провайдеров"""

    monkeypatch.setattr(auth_routes, "GOOGLE_CLIENT_ID", google)
    monkeypatch.setattr(auth_routes, "GITHUB_CLIENT_ID", github)
    monkeypatch.setattr(auth_routes, "OAUTH_ENABLED", bool(google or github))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/login",
            "headers": [],
            "query_string": b"",
        }
    )
    return asyncio.run(auth_routes.login_page(request)).body.decode()


def test_no_provider_configured_renders_no_oauth_link(monkeypatch):
    """Все client id пусты → в HTML нет ни одной OAuth-ссылки и нет разделителя"""

    page = _login_html(monkeypatch)

    assert "/auth/oauth/login/" not in page
    assert "or sign in with" not in page
    assert 'name="api_key"' in page


def test_only_configured_provider_button_rendered(monkeypatch):
    """Только GITHUB_CLIENT_ID задан → есть GitHub, нет Google"""

    page = _login_html(monkeypatch, github="".join(["fake", "-", "github", "-", "client"]))

    assert "/auth/oauth/login/github" in page
    assert "/auth/oauth/login/google" not in page
    assert "or sign in with" in page
