"""Support Chat — H2a trusted support identity, credentials and sessions.

H2a authority boundary:

  * a support identity is a dedicated DB row (``support_identities``) with
    explicit ``support_identity_memberships`` rows — one per tenant, role
    ``support_agent``. There is no wildcard/global tenant scope and no
    tenant-admin/super-admin flag;
  * a credential is an owner-provisioned opaque high-entropy token; only its
    SHA-256 digest is stored, and the raw value is printed once by the offline
    provisioning CLI;
  * exchanging the credential over HTTPS creates a server-side session with a
    fixed, non-sliding one-hour TTL. Only the session and CSRF digests are
    stored; the raw session travels in a Secure/HttpOnly/SameSite=Strict cookie
    and the raw CSRF value is returned once in the login response;
  * nothing in this module ever puts a raw credential, session token or CSRF
    value into a principal, an actor id, an exception message or a log line.

Every DB call is synchronous SQLAlchemy and is therefore executed through
``asyncio.to_thread`` so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic import SecretStr
from structlog import get_logger

from support_chat.db import get_session_factory
from support_chat.db_models import (
    SupportCredentialModel,
    SupportIdentityMembershipModel,
    SupportIdentityModel,
    SupportSessionModel,
)

logger = get_logger(__name__)

SUPPORT_AGENT_ROLE = "support_agent"
AUTH_METHOD_SUPPORT_SESSION = "support_session"
SUPPORT_SESSION_ISSUER = "roma-execution-bridge"
SUPPORT_SESSION_AUDIENCE = "support-api"

SESSION_COOKIE_NAME = "support_session"
SESSION_COOKIE_PATH = "/v1/support"
CSRF_HEADER_NAME = "X-CSRF-Token"
SESSION_TTL_SECONDS = 3600

# 48 random bytes = 384 bits, comfortably above the required 256-bit floor.
TOKEN_BYTES = 48
CREDENTIAL_BYTES = 48

ORIGINS_ENV_VAR = "SUPPORT_AUTH_ALLOWED_ORIGINS"


class SupportAuthError(ValueError):
    """Credential or session could not be authenticated (fail-closed)."""


class SupportCredentialError(SupportAuthError):
    """Credential is missing, unknown, expired or revoked."""


class SupportSessionError(SupportAuthError):
    """Session is missing, unknown, expired, revoked or version-mismatched."""


class SupportBrowserGuardError(SupportAuthError):
    """Origin/CSRF guard refused a cookie-authenticated operation."""


def generate_token(nbytes: int = TOKEN_BYTES) -> str:
    """High-entropy opaque token (>=256 bits) — the only place tokens are made."""
    return secrets.token_urlsafe(nbytes)


def hash_token(raw_token: str) -> str:
    """Deterministic lookup digest. Standard-library SHA-256, no pepper."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def allowed_origins() -> list[str]:
    """Exact Origin allowlist from ``SUPPORT_AUTH_ALLOWED_ORIGINS``.

    Fail-closed: an unset/empty/whitespace value yields ``[]``, which means
    browser login and cookie-authenticated mutations stay unavailable instead of
    accepting any origin. There is no ``*`` behaviour here.
    """
    raw = os.environ.get(ORIGINS_ENV_VAR, "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def origin_allowed(origin: str | None) -> bool:
    allowed = allowed_origins()
    if not allowed or not origin:
        return False
    return any(constant_time_equal(origin, candidate) for candidate in allowed)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _raw_secret(value: str | SecretStr | None) -> str:
    """Normalise a credential field without ever returning it to a caller."""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value or ""


@dataclass(frozen=True)
class SupportPrincipal:
    """Immutable trusted principal, built only from server-side DB rows.

    Carries no credential, session token or CSRF value: ``session_id`` is a
    non-secret row identifier used for audit attribution only.
    """

    actor_id: str
    identity_id: str
    roles: frozenset[str]
    tenant_ids: frozenset[str]
    session_id: str
    session_version: int
    auth_method: str
    issued_at: datetime
    expires_at: datetime
    issuer: str = SUPPORT_SESSION_ISSUER
    audience: str = SUPPORT_SESSION_AUDIENCE

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def scopes(self) -> frozenset[str]:
        return self.tenant_ids


class SupportAuthService:
    """Credential exchange, session verification, CSRF binding and revocation.

    The public methods are async wrappers that offload the whole transaction to
    a worker thread; the ``*_sync`` cores own their Session end to end.
    """

    def __init__(self, session_factory=None, now=None) -> None:
        self._session_factory = session_factory
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _factory(self):
        if self._session_factory is None:
            self._session_factory = get_session_factory()
        return self._session_factory

    async def login(
        self, credential: str | SecretStr | None
    ) -> tuple[SupportPrincipal, str, str]:
        """Exchange a credential for a session.

        Returns ``(principal, raw_session_token, raw_csrf_token)``. Raises
        :class:`SupportCredentialError` for any failure — the message never
        echoes the presented value.
        """
        return await asyncio.to_thread(self._login_sync, _raw_secret(credential))

    async def authenticate_session(self, raw_session_token: str | None) -> SupportPrincipal:
        return await asyncio.to_thread(
            self._authenticate_session_sync, raw_session_token
        )

    async def logout(self, raw_session_token: str | None) -> bool:
        """Revoke exactly the presented session. Idempotent."""
        return await asyncio.to_thread(self._logout_sync, raw_session_token)

    async def csrf_matches(self, raw_csrf_token: str | None, session_id: str) -> bool:
        """Constant-time check that a CSRF value belongs to this session row."""
        return await asyncio.to_thread(
            self._csrf_matches_sync, raw_csrf_token, session_id
        )

    def _login_sync(self, raw_credential: str) -> tuple[SupportPrincipal, str, str]:
        if not raw_credential or not isinstance(raw_credential, str):
            raise SupportCredentialError("credential is required")
        digest = hash_token(raw_credential)
        now = self._now()
        with self._factory()() as session:
            credential = (
                session.query(SupportCredentialModel)
                .filter(SupportCredentialModel.credential_hash == digest)
                .one_or_none()
            )
            if credential is None:
                raise SupportCredentialError("credential is not recognised")
            if credential.revoked_at is not None:
                raise SupportCredentialError("credential is revoked")
            expires_at = _as_utc(credential.expires_at)
            if expires_at is None or expires_at <= now:
                raise SupportCredentialError("credential is expired")

            identity = session.get(SupportIdentityModel, credential.identity_id)
            if identity is None or not identity.is_active:
                raise SupportCredentialError("identity is not active")

            memberships = self._active_memberships(session, identity.identity_id)
            if not memberships:
                raise SupportCredentialError("identity has no active tenant membership")

            raw_session_token = generate_token()
            raw_csrf_token = generate_token()
            session_row = SupportSessionModel(
                identity_id=identity.identity_id,
                session_hash=hash_token(raw_session_token),
                csrf_hash=hash_token(raw_csrf_token),
                session_version=int(identity.session_version),
                issued_at=now,
                expires_at=now + timedelta(seconds=SESSION_TTL_SECONDS),
            )
            session.add(session_row)
            credential.last_used_at = now
            session.commit()
            session.refresh(session_row)
            principal = self._build_principal(
                identity=identity,
                session_row=session_row,
                memberships=memberships,
            )
        logger.info(
            "support_credential_exchanged",
            identity_id=principal.identity_id,
            session_id=principal.session_id,
        )
        return principal, raw_session_token, raw_csrf_token

    def _authenticate_session_sync(self, raw_session_token: str | None) -> SupportPrincipal:
        if not raw_session_token or not isinstance(raw_session_token, str):
            raise SupportSessionError("session is required")
        digest = hash_token(raw_session_token)
        now = self._now()
        with self._factory()() as session:
            session_row = self._session_by_hash(session, digest)
            if session_row is None:
                raise SupportSessionError("session is not recognised")
            if session_row.revoked_at is not None:
                raise SupportSessionError("session is revoked")
            expires_at = _as_utc(session_row.expires_at)
            if expires_at is None or expires_at <= now:
                raise SupportSessionError("session is expired")

            identity = session.get(SupportIdentityModel, session_row.identity_id)
            if identity is None or not identity.is_active:
                raise SupportSessionError("identity is not active")
            if int(session_row.session_version) != int(identity.session_version):
                raise SupportSessionError("session was invalidated")

            memberships = self._active_memberships(session, identity.identity_id)
            if not memberships:
                raise SupportSessionError("identity has no active tenant membership")
            # The TTL is fixed at issuance and never extended here.
            return self._build_principal(
                identity=identity,
                session_row=session_row,
                memberships=memberships,
            )

    def _logout_sync(self, raw_session_token: str | None) -> bool:
        if not raw_session_token:
            return False
        digest = hash_token(raw_session_token)
        with self._factory()() as session:
            session_row = self._session_by_hash(session, digest)
            if session_row is None:
                return False
            if session_row.revoked_at is None:
                session_row.revoked_at = self._now()
                session.commit()
            return True

    def _csrf_matches_sync(self, raw_csrf_token: str | None, session_id: str) -> bool:
        if not raw_csrf_token or not session_id:
            return False
        try:
            session_uuid = uuid.UUID(str(session_id))
        except (ValueError, AttributeError, TypeError):
            return False
        with self._factory()() as session:
            session_row = session.get(SupportSessionModel, session_uuid)
            if session_row is None:
                return False
            return constant_time_equal(
                str(session_row.csrf_hash), hash_token(raw_csrf_token)
            )

    def bump_session_version(self, identity_id) -> int:
        """Global invalidation: every existing session stops verifying."""
        with self._factory()() as session:
            identity = session.get(SupportIdentityModel, identity_id)
            if identity is None:
                raise SupportAuthError("support identity not found")
            identity.session_version = int(identity.session_version) + 1
            identity.updated_at = self._now()
            session.commit()
            return int(identity.session_version)

    @staticmethod
    def _session_by_hash(session, digest: str) -> SupportSessionModel | None:
        return (
            session.query(SupportSessionModel)
            .filter(SupportSessionModel.session_hash == digest)
            .one_or_none()
        )

    @staticmethod
    def _active_memberships(session, identity_id) -> list[SupportIdentityMembershipModel]:
        return (
            session.query(SupportIdentityMembershipModel)
            .filter(
                SupportIdentityMembershipModel.identity_id == identity_id,
                SupportIdentityMembershipModel.is_active.is_(True),
                SupportIdentityMembershipModel.role == SUPPORT_AGENT_ROLE,
            )
            .all()
        )

    @staticmethod
    def _build_principal(
        identity: SupportIdentityModel,
        session_row: SupportSessionModel,
        memberships: list[SupportIdentityMembershipModel],
    ) -> SupportPrincipal:
        tenant_ids = frozenset(
            membership.tenant_id.strip()
            for membership in memberships
            if membership.tenant_id and membership.tenant_id.strip()
        )
        if not tenant_ids:
            raise SupportSessionError("identity has no explicit tenant scope")
        return SupportPrincipal(
            actor_id=f"support_agent:{identity.identity_id}",
            identity_id=str(identity.identity_id),
            roles=frozenset({SUPPORT_AGENT_ROLE}),
            tenant_ids=tenant_ids,
            session_id=str(session_row.session_id),
            session_version=int(session_row.session_version),
            auth_method=AUTH_METHOD_SUPPORT_SESSION,
            issued_at=_as_utc(session_row.issued_at),
            expires_at=_as_utc(session_row.expires_at),
        )
