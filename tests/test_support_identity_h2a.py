"""H2a — trusted support identity, least-privilege REST and offline provisioning.

Everything here runs against isolated SQLite/temp storage: no production DSN, no
network, no real credential, no app server. The contract under test:

  * a support identity is a DB row with explicit ``support_agent`` memberships;
  * credentials and sessions are stored as SHA-256 digests only;
  * ``assign`` is always a self-claim by the authenticated principal;
  * ``transition`` is allowed only for the ticket's current assignee;
  * a tenant API key stays a fail-closed 403 on those routes;
  * the WebSocket stays an unconditional pre-accept 4401.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import update
from starlette.websockets import WebSocketDisconnect

import support_chat.router as support_router
from support_chat import identity_admin
from support_chat.chat_service import ChatService
from support_chat.db import get_session_factory
from support_chat.db_models import (
    Base,
    SupportCredentialModel,
    SupportIdentityMembershipModel,
    SupportIdentityModel,
    SupportSessionModel,
    SupportTicketModel,
)
from support_chat.identity import (
    CREDENTIAL_BYTES,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    SUPPORT_AGENT_ROLE,
    SupportAuthService,
    SupportCredentialError,
    SupportSessionError,
    generate_token,
    hash_token,
    origin_allowed,
)
from support_chat.models import CreateTicketRequest, TicketStatus
from support_chat.service import (
    SupportTicketService,
    TenantContext,
    TicketNotFoundError,
    TicketStatusConflictError,
)

ORIGIN = "https://support.example.test"
RAW_KEY = "tenant-key-h2a"
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _ctx(tenant_id: str) -> TenantContext:
    return TenantContext.from_principal({"tenant_id": tenant_id, "name": tenant_id})


def _factory(tmp_path, name: str):
    factory = get_session_factory(f"sqlite:///{tmp_path}/{name}.db")
    with factory() as session:
        Base.metadata.create_all(session.get_bind())
    return factory


def _provision(factory, subject: str, tenants, **kwargs) -> dict:
    return identity_admin.create_identity(factory, subject, tenants, **kwargs)


class _ForbiddenService:
    """Any route use of the service fails the test loudly."""

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise AssertionError(f"service.{name} must not be reached")

        return _boom


@pytest.fixture
def h2a(tmp_path, monkeypatch):
    """Support app + auth service + ticket service sharing one SQLite file."""
    monkeypatch.setenv("SUPPORT_AUTH_ALLOWED_ORIGINS", ORIGIN)
    factory = _factory(tmp_path, "h2a")
    chat = ChatService()
    service = SupportTicketService(chat_service=chat, session_factory=factory)
    auth = SupportAuthService(session_factory=factory)
    monkeypatch.setattr(support_router, "_service", service)
    monkeypatch.setattr(support_router, "_auth", auth)
    app = FastAPI()
    app.include_router(support_router.router)
    client = TestClient(app, base_url="https://testserver")
    return {
        "factory": factory,
        "service": service,
        "chat": chat,
        "auth": auth,
        "client": client,
        "app": app,
    }


def _login(client, credential: str, origin: str | None = ORIGIN):
    headers = {"Origin": origin} if origin else {}
    resp = client.post(
        "/v1/support/auth/login", json={"credential": credential}, headers=headers
    )
    return resp


def _session_client(h2a, subject: str, tenants=(TENANT_A,)):
    """Log in and return (client, principal-ish login body, raw credential)."""
    info = _provision(h2a["factory"], subject, list(tenants))
    resp = _login(h2a["client"], info["credential"])
    assert resp.status_code == 200, resp.text
    return h2a["client"], resp.json(), info["credential"]


class TestCredentialAndSessionPersistence:
    """Only digests are stored; principals and errors carry no token material."""

    def test_01_credential_is_stored_as_a_digest_only(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-01", [TENANT_A])
        raw = info["credential"]
        with h2a["factory"]() as session:
            row = session.query(SupportCredentialModel).one()
            assert row.credential_hash == hash_token(raw)
            assert raw not in row.credential_hash
            dumped = {c.name: getattr(row, c.name) for c in row.__table__.columns}
            assert raw not in repr(dumped)

    def test_02_session_and_csrf_are_stored_as_digests_only(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-02", [TENANT_A])
        principal, raw_session, raw_csrf = h2a["auth"]._login_sync(info["credential"])
        assert raw_session and raw_csrf
        with h2a["factory"]() as session:
            row = session.query(SupportSessionModel).one()
            assert row.session_hash == hash_token(raw_session)
            assert row.csrf_hash == hash_token(raw_csrf)
            dumped = {c.name: getattr(row, c.name) for c in row.__table__.columns}
            blob = repr(dumped)
            assert raw_session not in blob
            assert raw_csrf not in blob
        assert raw_session not in repr(principal)
        assert raw_csrf not in repr(principal)
        assert raw_session not in principal.actor_id
        assert raw_csrf not in principal.actor_id

    def test_03_every_persisted_column_is_free_of_raw_token_material(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-03", [TENANT_A, TENANT_B])
        _, raw_session, raw_csrf = h2a["auth"]._login_sync(info["credential"])
        raw = info["credential"]
        with h2a["factory"]() as session:
            for model in (
                SupportIdentityModel,
                SupportIdentityMembershipModel,
                SupportCredentialModel,
                SupportSessionModel,
            ):
                rows = session.query(model).all()
                blob = repr(
                    [
                        {c.name: getattr(r, c.name) for c in r.__table__.columns}
                        for r in rows
                    ]
                )
                for secret in (raw, raw_session, raw_csrf):
                    assert secret not in blob, model.__tablename__

        # ORM metadata must mirror migration 014's ON DELETE CASCADE clauses.
        for model in (
            SupportIdentityMembershipModel,
            SupportCredentialModel,
            SupportSessionModel,
        ):
            fk = next(iter(model.__table__.c.identity_id.foreign_keys))
            assert fk.target_fullname == "support_identities.identity_id"
            assert fk.ondelete == "CASCADE", model.__tablename__

    def test_04_token_generation_is_cryptographically_secure(self, monkeypatch) -> None:
        calls: list[int] = []
        import secrets as secrets_module

        real = secrets_module.token_urlsafe

        def _spy(nbytes=None):
            calls.append(nbytes)
            return real(nbytes)

        monkeypatch.setattr(secrets_module, "token_urlsafe", _spy)
        token = generate_token()
        assert calls == [CREDENTIAL_BYTES]
        assert CREDENTIAL_BYTES * 8 >= 256
        assert len(token) >= 43
        assert len({generate_token() for _ in range(64)}) == 64

    def test_05_missing_unknown_and_malformed_credentials_fail_closed(self, h2a) -> None:
        auth = h2a["auth"]
        for bad in ("", "   ", None, "not-a-credential"):
            with pytest.raises(SupportCredentialError):
                auth._login_sync(bad)
        with pytest.raises(SupportCredentialError):
            auth._login_sync(generate_token())

    def test_06_expired_and_revoked_credentials_fail_closed(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-06", [TENANT_A])
        with h2a["factory"]() as session:
            row = session.query(SupportCredentialModel).one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()
        with pytest.raises(SupportCredentialError):
            h2a["auth"]._login_sync(info["credential"])

        info2 = _provision(h2a["factory"], "agent-06b", [TENANT_A])
        with h2a["factory"]() as session:
            row = session.query(SupportCredentialModel).filter(
                SupportCredentialModel.credential_hash == hash_token(info2["credential"])
            ).one()
            row.revoked_at = datetime.now(timezone.utc)
            session.commit()
        with pytest.raises(SupportCredentialError):
            h2a["auth"]._login_sync(info2["credential"])

    def test_07_inactive_identity_fails_closed(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-07", [TENANT_A])
        identity_admin.deactivate_identity(h2a["factory"], "agent-07")
        with pytest.raises(SupportCredentialError):
            h2a["auth"]._login_sync(info["credential"])

    def test_08_membership_absent_or_inactive_fails_closed(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-08", [TENANT_A])
        with h2a["factory"]() as session:
            row = session.query(SupportIdentityMembershipModel).one()
            row.is_active = False
            session.commit()
        with pytest.raises(SupportCredentialError):
            h2a["auth"]._login_sync(info["credential"])

    def test_09_scope_comes_from_explicit_membership_rows_only(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-09", [TENANT_A, TENANT_B])
        principal, _, _ = h2a["auth"]._login_sync(info["credential"])
        assert principal.tenant_ids == frozenset({TENANT_A, TENANT_B})
        assert "*" not in principal.tenant_ids
        assert "" not in principal.tenant_ids
        assert principal.roles == frozenset({SUPPORT_AGENT_ROLE})

    def test_10_session_is_one_hour_and_does_not_slide(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-10", [TENANT_A])
        principal, raw_session, _ = h2a["auth"]._login_sync(info["credential"])
        ttl = principal.expires_at - principal.issued_at
        assert ttl == timedelta(seconds=SESSION_TTL_SECONDS)

        with h2a["factory"]() as session:
            before = session.query(SupportSessionModel).one().expires_at
        h2a["auth"]._authenticate_session_sync(raw_session)
        with h2a["factory"]() as session:
            after = session.query(SupportSessionModel).one().expires_at
        assert after == before

    def test_11_expired_revoked_and_version_mismatched_sessions_fail_closed(
        self, h2a
    ) -> None:
        info = _provision(h2a["factory"], "agent-11", [TENANT_A])
        _, raw_session, _ = h2a["auth"]._login_sync(info["credential"])

        with h2a["factory"]() as session:
            row = session.query(SupportSessionModel).one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()
        with pytest.raises(SupportSessionError):
            h2a["auth"]._authenticate_session_sync(raw_session)

        with h2a["factory"]() as session:
            row = session.query(SupportSessionModel).one()
            row.expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
            row.session_version = 99
            session.commit()
        with pytest.raises(SupportSessionError):
            h2a["auth"]._authenticate_session_sync(raw_session)

        with h2a["factory"]() as session:
            row = session.query(SupportSessionModel).one()
            row.session_version = 1
            row.revoked_at = datetime.now(timezone.utc)
            session.commit()
        with pytest.raises(SupportSessionError):
            h2a["auth"]._authenticate_session_sync(raw_session)

    def test_12_session_version_bump_invalidates_live_sessions(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-12", [TENANT_A])
        _, raw_session, _ = h2a["auth"]._login_sync(info["credential"])
        h2a["auth"]._authenticate_session_sync(raw_session)

        identity_admin.revoke_sessions(h2a["factory"], "agent-12")
        with pytest.raises(SupportSessionError):
            h2a["auth"]._authenticate_session_sync(raw_session)

    def test_13_logout_revokes_the_presented_session(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-13", [TENANT_A])
        _, raw_session, _ = h2a["auth"]._login_sync(info["credential"])
        assert h2a["auth"]._logout_sync(raw_session) is True
        with pytest.raises(SupportSessionError):
            h2a["auth"]._authenticate_session_sync(raw_session)
        # Idempotent: a second logout on the same (revoked) session is not an error.
        assert h2a["auth"]._logout_sync(raw_session) is True
        assert h2a["auth"]._logout_sync(None) is False

    def test_14_unknown_session_token_fails_closed(self, h2a) -> None:
        for bad in (None, "", generate_token()):
            with pytest.raises(SupportSessionError):
                h2a["auth"]._authenticate_session_sync(bad)


class TestBrowserGuards:
    """Origin allowlist is fail-closed; CSRF is bound to the session row."""

    def test_15_origin_allowlist_is_fail_closed(self, monkeypatch) -> None:
        monkeypatch.delenv("SUPPORT_AUTH_ALLOWED_ORIGINS", raising=False)
        assert origin_allowed(ORIGIN) is False
        monkeypatch.setenv("SUPPORT_AUTH_ALLOWED_ORIGINS", "  ")
        assert origin_allowed(ORIGIN) is False
        monkeypatch.setenv("SUPPORT_AUTH_ALLOWED_ORIGINS", "*")
        assert origin_allowed(ORIGIN) is False
        monkeypatch.setenv("SUPPORT_AUTH_ALLOWED_ORIGINS", ORIGIN)
        assert origin_allowed(ORIGIN) is True
        assert origin_allowed("https://evil.example.test") is False
        assert origin_allowed(None) is False

    def test_16_login_requires_an_allowed_origin(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-16", [TENANT_A])
        assert _login(h2a["client"], info["credential"], origin=None).status_code == 403
        assert (
            _login(h2a["client"], info["credential"], origin="https://evil.test").status_code
            == 403
        )
        ok = _login(h2a["client"], info["credential"])
        assert ok.status_code == 200

    def test_17_login_sets_a_hardened_cookie_and_returns_csrf_only(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-17", [TENANT_A])
        resp = _login(h2a["client"], info["credential"])
        assert resp.status_code == 200
        cookie = resp.headers["set-cookie"]
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie.lower().replace("samesite=strict", "SameSite=strict")
        assert "Path=/v1/support" in cookie
        assert f"Max-Age={SESSION_TTL_SECONDS}" in cookie
        body = resp.json()
        assert body["csrf_token"]
        assert SESSION_COOKIE_NAME not in resp.text
        assert info["credential"] not in resp.text
        assert cookie.split(";")[0].split("=", 1)[1] not in resp.text

    def test_18_csrf_is_required_and_bound_to_the_session(self, h2a, monkeypatch) -> None:
        info_a = _provision(h2a["factory"], "agent-18", [TENANT_A])
        login_a = _login(h2a["client"], info_a["credential"])
        assert login_a.status_code == 200, login_a.text
        csrf_a = login_a.json()["csrf_token"]
        assert csrf_a

        # The CSRF token returned by the HTTP login must be bound to the session
        # row of the very cookie that same HTTP response set — not to any other
        # session, and not to a separately minted one.
        cookie_a = h2a["client"].cookies.get(SESSION_COOKIE_NAME)
        assert cookie_a
        principal_a = h2a["auth"]._authenticate_session_sync(cookie_a)
        assert h2a["auth"]._csrf_matches_sync(csrf_a, principal_a.session_id) is True

        # A CSRF token from a *different* exchange never matches this session.
        _, _, other_csrf = h2a["auth"]._login_sync(info_a["credential"])
        assert h2a["auth"]._csrf_matches_sync(other_csrf, principal_a.session_id) is False

        assert h2a["auth"]._csrf_matches_sync("wrong", principal_a.session_id) is False
        assert h2a["auth"]._csrf_matches_sync(None, principal_a.session_id) is False
        assert h2a["auth"]._csrf_matches_sync(csrf_a, "not-a-uuid") is False
        assert h2a["auth"]._csrf_matches_sync(csrf_a, str(uuid.uuid4())) is False

        # A second, independent browser session with its own cookie jar.
        info_b = _provision(h2a["factory"], "agent-18-b", [TENANT_A])
        client_b = TestClient(h2a["app"], base_url="https://testserver")
        login_b = _login(client_b, info_b["credential"])
        assert login_b.status_code == 200, login_b.text
        cookie_b = client_b.cookies.get(SESSION_COOKIE_NAME)
        assert cookie_b and cookie_b != cookie_a

        # Privileged mutations must fail closed on every browser-guard error and
        # must never reach the service layer.
        monkeypatch.setattr(support_router, "_service", _ForbiddenService())

        for name, path, payload in (
            ("assign", f"/v1/support/tickets/{uuid.uuid4()}/assign", {}),
            (
                "transition",
                f"/v1/support/tickets/{uuid.uuid4()}/transition?status=closed",
                None,
            ),
        ):
            cases = (
                ("missing-csrf", h2a["client"], {"Origin": ORIGIN}, payload),
                (
                    "invalid-csrf",
                    h2a["client"],
                    {"Origin": ORIGIN, CSRF_HEADER_NAME: "wrong"},
                    payload,
                ),
                (
                    "bad-origin",
                    h2a["client"],
                    {"Origin": "https://evil.test", CSRF_HEADER_NAME: csrf_a},
                    payload,
                ),
                (
                    "cross-session-csrf",
                    client_b,
                    {"Origin": ORIGIN, CSRF_HEADER_NAME: csrf_a},
                    payload,
                ),
            )
            for label, client, headers, body in cases:
                if body is None:
                    resp = client.post(path, headers=headers)
                else:
                    resp = client.post(path, json=body, headers=headers)
                assert resp.status_code == 403, (
                    f"{name}/{label}: {resp.status_code} {resp.text}"
                )

    def test_19_logout_needs_origin_and_csrf(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-19", [TENANT_A])
        login = _login(h2a["client"], info["credential"])
        csrf = login.json()["csrf_token"]

        missing_csrf = h2a["client"].post(
            "/v1/support/auth/logout", headers={"Origin": ORIGIN}
        )
        assert missing_csrf.status_code == 403

        wrong_csrf = h2a["client"].post(
            "/v1/support/auth/logout",
            headers={"Origin": ORIGIN, CSRF_HEADER_NAME: "wrong"},
        )
        assert wrong_csrf.status_code == 403

        wrong_origin = h2a["client"].post(
            "/v1/support/auth/logout",
            headers={"Origin": "https://evil.test", CSRF_HEADER_NAME: csrf},
        )
        assert wrong_origin.status_code == 403

        ok = h2a["client"].post(
            "/v1/support/auth/logout",
            headers={"Origin": ORIGIN, CSRF_HEADER_NAME: csrf},
        )
        assert ok.status_code == 204
        assert h2a["client"].get("/v1/support/auth/session").status_code == 401


class TestProvisioningCli:
    """Offline provisioning: one-time credential output, atomic failures."""

    def test_20_create_returns_credential_once_and_stores_digests(self, h2a) -> None:
        info = _provision(
            h2a["factory"], "cli-agent", [TENANT_A, TENANT_B], expires_in_days=30
        )
        assert info["credential"]
        assert info["role"] == SUPPORT_AGENT_ROLE
        assert info["tenant_ids"] == [TENANT_A, TENANT_B]
        with h2a["factory"]() as session:
            identity = session.query(SupportIdentityModel).one()
            assert identity.subject == "cli-agent"
            memberships = session.query(SupportIdentityMembershipModel).all()
            assert {m.tenant_id for m in memberships} == {TENANT_A, TENANT_B}
            assert {m.role for m in memberships} == {SUPPORT_AGENT_ROLE}
            credential = session.query(SupportCredentialModel).one()
            assert credential.credential_hash == hash_token(info["credential"])
        # The raw credential is never recoverable from the database.
        with h2a["factory"]() as session:
            blob = repr(
                [
                    {c.name: getattr(r, c.name) for c in r.__table__.columns}
                    for r in session.query(SupportCredentialModel).all()
                ]
            )
        assert info["credential"] not in blob

    def test_21_duplicate_subject_and_duplicate_membership_fail_atomically(
        self, h2a
    ) -> None:
        _provision(h2a["factory"], "dupe", [TENANT_A])
        with pytest.raises(identity_admin.SupportAuthError):
            _provision(h2a["factory"], "dupe", [TENANT_B])

        with pytest.raises(identity_admin.SupportAuthError):
            _provision(h2a["factory"], "dupe-membership", [TENANT_A, TENANT_A])

        with h2a["factory"]() as session:
            assert session.query(SupportIdentityModel).count() == 1

    def test_22_tenant_and_expiry_validation_is_required(self, h2a) -> None:
        with pytest.raises(identity_admin.SupportAuthError):
            _provision(h2a["factory"], "no-tenant", [])
        with pytest.raises(identity_admin.SupportAuthError):
            _provision(h2a["factory"], "blank-tenant", ["   "])
        with pytest.raises(identity_admin.SupportAuthError):
            _provision(h2a["factory"], "   ", [TENANT_A])
        with pytest.raises(identity_admin.SupportAuthError):
            _provision(h2a["factory"], "bad-expiry", [TENANT_A], expires_in_days=0)
        with pytest.raises(identity_admin.SupportAuthError):
            _provision(
                h2a["factory"],
                "past-expiry",
                [TENANT_A],
                expires_at="2000-01-01T00:00:00Z",
            )
        with h2a["factory"]() as session:
            assert session.query(SupportIdentityModel).count() == 0

    def test_23_revoke_credential_by_id_or_subject(self, h2a) -> None:
        first = _provision(h2a["factory"], "revoke-a", [TENANT_A])
        _provision(h2a["factory"], "revoke-b", [TENANT_A])

        assert identity_admin.revoke_credential(
            h2a["factory"], credential_id=first["credential_id"]
        ) == 1
        with pytest.raises(SupportCredentialError):
            h2a["auth"]._login_sync(first["credential"])

        assert identity_admin.revoke_credential(h2a["factory"], subject="revoke-b") == 1
        with pytest.raises(identity_admin.SupportAuthError):
            identity_admin.revoke_credential(h2a["factory"])
        with pytest.raises(SupportCredentialError):
            identity_admin.revoke_credential(h2a["factory"], credential_id="not-a-uuid")

    def test_24_revoke_sessions_and_deactivate(self, h2a) -> None:
        info = _provision(h2a["factory"], "cli-revoke", [TENANT_A])
        _, raw_session, _ = h2a["auth"]._login_sync(info["credential"])

        result = identity_admin.revoke_sessions(h2a["factory"], "cli-revoke")
        assert result["session_version"] == 2
        assert result["revoked_sessions"] == 1
        with pytest.raises(SupportSessionError):
            h2a["auth"]._authenticate_session_sync(raw_session)

        assert identity_admin.deactivate_identity(h2a["factory"], "cli-revoke") is True
        with pytest.raises(SupportCredentialError):
            h2a["auth"]._login_sync(info["credential"])

    def test_25_cli_uses_only_the_explicit_local_database(self, tmp_path) -> None:
        """The CLI runs on an explicit SQLite URL — never on a production DSN."""
        from scripts import support_identity_admin as cli

        url = f"sqlite:///{tmp_path}/cli.db"
        rc = cli.main(
            [
                "--db-url",
                url,
                "create",
                "--subject",
                "cli-only",
                "--tenant-id",
                TENANT_A,
            ]
        )
        assert rc == 0
        factory = get_session_factory(url)
        with factory() as session:
            assert session.query(SupportIdentityModel).count() == 1


class TestRestLeastPrivilege:
    """HTTP contract: 401/400/403 ordering, self-claim only, no caller agent id."""

    def test_26_no_credentials_is_401_before_any_service_call(
        self, h2a, monkeypatch
    ) -> None:
        monkeypatch.setattr(support_router, "_service", _ForbiddenService())
        resp = h2a["client"].post(
            f"/v1/support/tickets/{uuid.uuid4()}/assign", json={}
        )
        assert resp.status_code == 401

    def test_27_invalid_tenant_key_is_401(self, h2a, monkeypatch) -> None:
        import db_adapter

        monkeypatch.setattr(db_adapter, "find_tenant_by_key", lambda key: None)
        monkeypatch.setattr(support_router, "_service", _ForbiddenService())
        resp = h2a["client"].post(
            f"/v1/support/tickets/{uuid.uuid4()}/assign",
            json={},
            headers={"X-API-Key": "bogus"},
        )
        assert resp.status_code == 401

    def test_28_valid_tenant_key_is_403_before_any_lookup(self, h2a, monkeypatch) -> None:
        import support_chat.router as router_module

        def _fake_key(request: Request):
            return (
                {"tenant_id": TENANT_A, "name": TENANT_A}
                if request.headers.get("x-api-key")
                else None
            )

        h2a["app"].dependency_overrides[router_module._optional_tenant_principal] = _fake_key
        monkeypatch.setattr(support_router, "_service", _ForbiddenService())

        # CR-1: the wrapper is deliberately synchronous, so FastAPI runs the
        # sync DB-backed canonical verifier in its dependency threadpool
        # instead of blocking the event loop.
        assert inspect.iscoroutinefunction(
            router_module._optional_tenant_principal
        ) is False

        assign = h2a["client"].post(
            f"/v1/support/tickets/{uuid.uuid4()}/assign",
            json={},
            headers={"X-API-Key": RAW_KEY},
        )
        assert assign.status_code == 403
        transition = h2a["client"].post(
            f"/v1/support/tickets/{uuid.uuid4()}/transition?status=closed",
            headers={"X-API-Key": RAW_KEY},
        )
        assert transition.status_code == 403

    def test_29_both_credential_types_are_rejected_before_lookup(
        self, h2a, monkeypatch
    ) -> None:
        """A session cookie plus an API key is refused with 400, before lookup."""
        import support_chat.router as router_module

        def _fake_key(request: Request):
            return (
                {"tenant_id": TENANT_A, "name": TENANT_A}
                if request.headers.get("x-api-key")
                else None
            )

        h2a["app"].dependency_overrides[router_module._optional_tenant_principal] = _fake_key
        monkeypatch.setattr(support_router, "_service", _ForbiddenService())

        # Establish a real session cookie first, then present both credentials.
        client, _, _ = _session_client(h2a, "agent-29")
        resp = client.post(
            f"/v1/support/tickets/{uuid.uuid4()}/assign",
            json={},
            headers={"X-API-Key": RAW_KEY},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_30_assign_takes_no_body_and_rejects_caller_agent_id(self, h2a) -> None:
        client, login, _ = _session_client(h2a, "agent-30")
        csrf = login["csrf_token"]
        headers = {"Origin": ORIGIN, CSRF_HEADER_NAME: csrf}

        ticket = await h2a["service"].create_ticket(
            CreateTicketRequest(subject="assign-body"), _ctx(TENANT_A)
        )
        tid = str(ticket.ticket_id)

        empty = client.post(f"/v1/support/tickets/{tid}/assign", json={}, headers=headers)
        assert empty.status_code == 200
        assert empty.json()["assigned_agent_id"] == login["actor_id"]

        no_body = client.post(f"/v1/support/tickets/{tid}/assign", headers=headers)
        assert no_body.status_code == 200
        assert no_body.json()["assigned_agent_id"] == login["actor_id"]

        with_agent = client.post(
            f"/v1/support/tickets/{tid}/assign",
            json={"agent_id": "someone-else"},
            headers=headers,
        )
        assert with_agent.status_code == 422

    @pytest.mark.asyncio
    async def test_31_foreign_tenant_ticket_is_a_non_enumerating_404(self, h2a) -> None:
        client, login, _ = _session_client(h2a, "agent-31", tenants=(TENANT_A,))
        csrf = login["csrf_token"]
        headers = {"Origin": ORIGIN, CSRF_HEADER_NAME: csrf}

        foreign = await h2a["service"].create_ticket(
            CreateTicketRequest(subject="foreign"), _ctx(TENANT_B)
        )
        resp = client.post(
            f"/v1/support/tickets/{foreign.ticket_id}/assign", json={}, headers=headers
        )
        assert resp.status_code == 404
        stored = await h2a["service"].get_ticket(str(foreign.ticket_id), _ctx(TENANT_B))
        assert stored.assigned_agent_id is None
        assert stored.status == TicketStatus.OPEN

    def test_32_forged_identity_headers_never_elevate(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-32", [TENANT_A])
        forged = {
            "Origin": ORIGIN,
            "x-tenant-id": TENANT_B,
            "x-user-id": "admin",
            "x-user-role": "super_admin",
        }
        resp = h2a["client"].post(
            "/v1/support/auth/login",
            json={"credential": info["credential"]},
            headers=forged,
        )
        assert resp.status_code == 200
        assert resp.json()["tenant_ids"] == [TENANT_A]

        session = h2a["client"].get("/v1/support/auth/session", headers=forged)
        assert session.status_code == 200
        assert session.json()["tenant_ids"] == [TENANT_A]
        assert session.json()["roles"] == [SUPPORT_AGENT_ROLE]

    @pytest.mark.asyncio
    async def test_33_transition_requires_the_current_assignee(self, h2a) -> None:
        client, login, _ = _session_client(h2a, "agent-33")
        csrf = login["csrf_token"]
        headers = {"Origin": ORIGIN, CSRF_HEADER_NAME: csrf}

        ticket = await h2a["service"].create_ticket(
            CreateTicketRequest(subject="transition"), _ctx(TENANT_A)
        )
        tid = str(ticket.ticket_id)

        claimed = client.post(
            f"/v1/support/tickets/{tid}/assign", json={}, headers=headers
        )
        assert claimed.status_code == 200

        moved = client.post(
            f"/v1/support/tickets/{tid}/transition?status=waiting_customer",
            headers=headers,
        )
        assert moved.status_code == 200
        assert moved.json()["status"] == TicketStatus.WAITING_CUSTOMER.value

    @pytest.mark.asyncio
    async def test_34_transition_by_another_agent_is_404_without_mutation(self, h2a) -> None:
        other = _provision(h2a["factory"], "agent-34-other", [TENANT_A])
        other_login = _login(h2a["client"], other["credential"])
        other_principal, _, _ = h2a["auth"]._login_sync(other["credential"])

        ticket = await h2a["service"].create_ticket(
            CreateTicketRequest(subject="assigned-elsewhere"), _ctx(TENANT_A)
        )
        await h2a["service"].assign_agent_to_self(
            str(ticket.ticket_id), "support_agent:someone-else", frozenset({TENANT_A})
        )

        with pytest.raises(TicketNotFoundError):
            await h2a["service"].transition_status_as_assignee(
                str(ticket.ticket_id),
                TicketStatus.WAITING_CUSTOMER,
                other_principal.actor_id,
                frozenset({TENANT_A}),
            )
        stored = await h2a["service"].get_ticket(
            str(ticket.ticket_id), _ctx(TENANT_A)
        )
        assert stored.status == TicketStatus.IN_PROGRESS
        assert other_login.status_code == 200

    @pytest.mark.asyncio
    async def test_35_service_level_e15_contract_is_preserved(self, h2a) -> None:
        service = h2a["service"]
        actor = "support_agent:agent-35"
        scopes = frozenset({TENANT_A})

        missing = uuid.uuid4()
        with pytest.raises(TicketNotFoundError):
            await service.assign_agent_to_self(str(missing), actor, scopes)

        ticket = await service.create_ticket(
            CreateTicketRequest(subject="e15"), _ctx(TENANT_A)
        )
        tid = str(ticket.ticket_id)

        first = await service.assign_agent_to_self(tid, actor, scopes)
        assert first.status == TicketStatus.IN_PROGRESS
        assert first.assigned_agent_id == actor

        # An already-IN_PROGRESS ticket may be re-claimed (accepted E-15 rule).
        again = await service.assign_agent_to_self(
            tid, "support_agent:other", scopes
        )
        assert again.status == TicketStatus.IN_PROGRESS
        assert again.assigned_agent_id == "support_agent:other"

        # A terminal status refuses assignment with the typed conflict.
        await service.transition_status_as_assignee(
            tid, TicketStatus.RESOLVED, "support_agent:other", scopes
        )
        with pytest.raises(TicketStatusConflictError):
            await service.assign_agent_to_self(tid, actor, scopes)

        # Out-of-scope tenant behaves exactly like a missing ticket.
        with pytest.raises(TicketNotFoundError):
            await service.assign_agent_to_self(tid, actor, frozenset({TENANT_B}))

    def test_36_websocket_still_closes_4401_before_accept(self, h2a) -> None:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with h2a["client"].websocket_connect(
                f"/v1/support/ws/{uuid.uuid4()}"
            ) as ws:
                ws.receive_text()
        assert excinfo.value.code == 4401


class TestNoSecretLeakage:
    """No credential/session/CSRF value may surface in errors or responses."""

    def test_37_login_failures_never_echo_the_presented_credential(self, h2a) -> None:
        presented = generate_token()
        resp = _login(h2a["client"], presented)
        assert resp.status_code == 401
        assert presented not in resp.text
        assert hash_token(presented) not in resp.text

    def test_38_session_endpoint_exposes_no_token_material(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-38", [TENANT_A])
        login = _login(h2a["client"], info["credential"])
        raw_session = login.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        csrf = login.json()["csrf_token"]

        resp = h2a["client"].get("/v1/support/auth/session")
        assert resp.status_code == 200
        assert raw_session not in resp.text
        assert csrf not in resp.text
        assert info["credential"] not in resp.text
        assert set(resp.json()) == {
            "actor_id",
            "roles",
            "tenant_ids",
            "auth_method",
            "issued_at",
            "expires_at",
        }

    def test_39_actor_id_is_stable_and_secret_free(self, h2a) -> None:
        info = _provision(h2a["factory"], "agent-39", [TENANT_A])
        principal, raw_session, raw_csrf = h2a["auth"]._login_sync(info["credential"])
        assert principal.actor_id == f"support_agent:{info['identity_id']}"
        assert principal.actor_id.startswith("support_agent:")
        for secret in (info["credential"], raw_session, raw_csrf):
            assert secret not in principal.actor_id
        assert principal.auth_method == "support_session"
        assert principal.issuer and principal.audience


class TestConcurrentCasConflicts:
    """The real CAS UPDATE must lose deterministically when the row moved (H2A-01).

    No fake ``rowcount`` and no production seam: the competing write happens on
    the service's own connection with ``synchronize_session=False``, so the row
    the service loaded stays stale and its real UPDATE matches zero rows.
    """

    @staticmethod
    def _hooked_service(factory, hook):
        class _HookedSession:
            def __init__(self, inner) -> None:
                self._inner = inner

            def get(self, entity, ident, **kwargs):
                obj = self._inner.get(entity, ident, **kwargs)
                if entity is SupportTicketModel and obj is not None:
                    hook(self._inner, obj)
                return obj

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

        class _HookedFactory:
            def __call__(self):
                return _HookedSession(factory())

        return SupportTicketService(
            chat_service=ChatService(), session_factory=_HookedFactory()
        )

    @staticmethod
    def _compete(session, tid, values) -> None:
        """Commit a competing write on the service's own connection.

        ``synchronize_session=False`` leaves the already-loaded row stale, and the
        commit makes the change durable — otherwise the service's own rollback on
        conflict would silently undo it and the real UPDATE would match again.
        """
        session.execute(
            update(SupportTicketModel)
            .where(SupportTicketModel.ticket_id == tid)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        session.commit()

    @pytest.mark.asyncio
    async def test_40_assignment_cas_loses_and_writes_nothing(self, h2a) -> None:
        scopes = frozenset({TENANT_A})
        actor = "support_agent:agent-40"
        ticket = await h2a["service"].create_ticket(
            CreateTicketRequest(subject="cas-assign"), _ctx(TENANT_A)
        )
        tid = ticket.ticket_id

        fired: list[str] = []

        def hook(session, model) -> None:
            if fired:
                return
            fired.append("competing-write")
            self._compete(session, tid, {"status": TicketStatus.CLOSED.value})

        messages_before = len(h2a["chat"].get_messages(str(tid)))
        hooked = self._hooked_service(h2a["factory"], hook)
        with pytest.raises(TicketStatusConflictError) as excinfo:
            await hooked.assign_agent_to_self(str(tid), actor, scopes)
        assert "concurrently" in str(excinfo.value)
        assert fired == ["competing-write"]

        with h2a["factory"]() as session:
            row = session.get(SupportTicketModel, tid)
            assert row.status == TicketStatus.CLOSED.value
            assert row.assigned_agent_id is None
            assert row.tenant_id == TENANT_A

        # The losing self-claim must not reach the participant/chat side effects.
        assert str(tid) not in hooked._participants
        assert len(h2a["chat"].get_messages(str(tid))) == messages_before

    @pytest.mark.asyncio
    async def test_41_transition_cas_loses_and_keeps_new_assignee(self, h2a) -> None:
        scopes = frozenset({TENANT_A})
        owner = "support_agent:agent-41"
        other = "support_agent:agent-41b"
        ticket = await h2a["service"].create_ticket(
            CreateTicketRequest(subject="cas-transition"), _ctx(TENANT_A)
        )
        tid = ticket.ticket_id
        await h2a["service"].assign_agent_to_self(str(tid), owner, scopes)

        fired: list[str] = []

        def hook(session, model) -> None:
            if fired:
                return
            fired.append("competing-write")
            self._compete(session, tid, {"assigned_agent_id": other})

        hooked = self._hooked_service(h2a["factory"], hook)
        with pytest.raises(ValueError) as excinfo:
            await hooked.transition_status_as_assignee(
                str(tid), TicketStatus.RESOLVED, owner, scopes
            )
        assert not isinstance(excinfo.value, TicketNotFoundError)
        assert "concurrently" in str(excinfo.value)
        assert fired == ["competing-write"]

        with h2a["factory"]() as session:
            row = session.get(SupportTicketModel, tid)
            assert row.assigned_agent_id == other
            assert row.status == TicketStatus.IN_PROGRESS.value
            assert row.resolved_at is None
            assert row.tenant_id == TENANT_A
