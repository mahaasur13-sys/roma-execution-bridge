"""Support Chat — H2a offline identity provisioning (no network, no admin API).

Deliberately operator-run and offline: there is no HTTP surface for creating
support identities or credentials. The raw credential is printed exactly once,
after a successful commit, and is never persisted, logged or returned again.
There is no seed identity, no default tenant and no wildcard scope.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from support_chat.identity import (
    CREDENTIAL_BYTES,
    SUPPORT_AGENT_ROLE,
    SupportAuthError,
    SupportCredentialError,
    _as_utc,
    generate_token,
    hash_token,
)
from support_chat.db_models import (
    SupportCredentialModel,
    SupportIdentityMembershipModel,
    SupportIdentityModel,
    SupportSessionModel,
)

DEFAULT_CREDENTIAL_TTL_DAYS = 90


def _clean_tenant_ids(tenant_ids) -> list[str]:
    cleaned: list[str] = []
    for raw in tenant_ids or ():
        tenant = str(raw).strip()
        if not tenant:
            raise SupportAuthError("tenant id must be non-empty")
        if tenant in cleaned:
            raise SupportAuthError(f"duplicate tenant membership: {tenant}")
        cleaned.append(tenant)
    if not cleaned:
        raise SupportAuthError("at least one explicit --tenant-id is required")
    return cleaned


def _clean_subject(subject: str) -> str:
    cleaned = str(subject or "").strip()
    if not cleaned:
        raise SupportAuthError("subject must be non-empty")
    return cleaned


def _resolve_expiry(expires_in_days: int | None, expires_at: str | None) -> datetime:
    if expires_at:
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SupportAuthError(f"invalid --expires-at: {expires_at}") from exc
        parsed = _as_utc(parsed) or parsed
        if parsed <= datetime.now(timezone.utc):
            raise SupportAuthError("--expires-at must be in the future")
        return parsed
    days = DEFAULT_CREDENTIAL_TTL_DAYS if expires_in_days is None else int(expires_in_days)
    if days <= 0:
        raise SupportAuthError("--expires-in-days must be positive")
    return datetime.now(timezone.utc) + timedelta(days=days)


def create_identity(
    factory,
    subject: str,
    tenant_ids,
    display_name: str | None = None,
    expires_in_days: int | None = None,
    expires_at: str | None = None,
) -> dict:
    """Create identity + explicit memberships + one credential, atomically.

    The raw credential is returned to the caller (to be printed once) only after
    the transaction committed; a failure leaves no partially provisioned rows.
    """
    subject = _clean_subject(subject)
    tenants = _clean_tenant_ids(tenant_ids)
    expiry = _resolve_expiry(expires_in_days, expires_at)
    now = datetime.now(timezone.utc)
    raw_credential = generate_token(CREDENTIAL_BYTES)

    with factory() as session:
        existing = (
            session.query(SupportIdentityModel)
            .filter(SupportIdentityModel.subject == subject)
            .one_or_none()
        )
        if existing is not None:
            raise SupportAuthError(f"support identity already exists: {subject}")
        identity = SupportIdentityModel(
            identity_id=uuid.uuid4(),
            subject=subject,
            display_name=display_name,
            is_active=True,
            session_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(identity)
        session.flush()
        for tenant in tenants:
            session.add(
                SupportIdentityMembershipModel(
                    membership_id=uuid.uuid4(),
                    identity_id=identity.identity_id,
                    tenant_id=tenant,
                    role=SUPPORT_AGENT_ROLE,
                    is_active=True,
                    created_at=now,
                )
            )
        credential = SupportCredentialModel(
            credential_id=uuid.uuid4(),
            identity_id=identity.identity_id,
            credential_hash=hash_token(raw_credential),
            expires_at=expiry,
            revoked_at=None,
            created_at=now,
            last_used_at=None,
        )
        session.add(credential)
        session.commit()
        identity_id = str(identity.identity_id)
        credential_id = str(credential.credential_id)

    return {
        "identity_id": identity_id,
        "subject": subject,
        "tenant_ids": tenants,
        "role": SUPPORT_AGENT_ROLE,
        "credential_id": credential_id,
        "credential_expires_at": expiry.isoformat(),
        "credential": raw_credential,
    }


def revoke_credential(factory, credential_id: str | None = None, subject: str | None = None) -> int:
    """Revoke credential rows by non-secret identifier (never by raw value)."""
    if not credential_id and not subject:
        raise SupportAuthError("provide --credential-id or --subject")
    now = datetime.now(timezone.utc)
    with factory() as session:
        query = session.query(SupportCredentialModel).filter(
            SupportCredentialModel.revoked_at.is_(None)
        )
        if credential_id:
            try:
                query = query.filter(
                    SupportCredentialModel.credential_id == uuid.UUID(str(credential_id))
                )
            except (ValueError, AttributeError, TypeError) as exc:
                raise SupportCredentialError("invalid --credential-id") from exc
        else:
            identity = (
                session.query(SupportIdentityModel)
                .filter(SupportIdentityModel.subject == _clean_subject(subject))
                .one_or_none()
            )
            if identity is None:
                raise SupportAuthError(f"support identity not found: {subject}")
            query = query.filter(
                SupportCredentialModel.identity_id == identity.identity_id
            )
        rows = query.all()
        for row in rows:
            row.revoked_at = now
        session.commit()
        return len(rows)


def revoke_sessions(factory, subject: str) -> dict:
    """Invalidate every session of an identity (version bump + explicit revoke)."""
    subject = _clean_subject(subject)
    now = datetime.now(timezone.utc)
    with factory() as session:
        identity = (
            session.query(SupportIdentityModel)
            .filter(SupportIdentityModel.subject == subject)
            .one_or_none()
        )
        if identity is None:
            raise SupportAuthError(f"support identity not found: {subject}")
        identity.session_version = int(identity.session_version) + 1
        identity.updated_at = now
        rows = (
            session.query(SupportSessionModel)
            .filter(
                SupportSessionModel.identity_id == identity.identity_id,
                SupportSessionModel.revoked_at.is_(None),
            )
            .all()
        )
        for row in rows:
            row.revoked_at = now
        session.commit()
        return {
            "subject": subject,
            "session_version": int(identity.session_version),
            "revoked_sessions": len(rows),
        }


def deactivate_identity(factory, subject: str) -> bool:
    """Deactivate an identity: credential exchange and sessions fail closed."""
    subject = _clean_subject(subject)
    now = datetime.now(timezone.utc)
    with factory() as session:
        identity = (
            session.query(SupportIdentityModel)
            .filter(SupportIdentityModel.subject == subject)
            .one_or_none()
        )
        if identity is None:
            raise SupportAuthError(f"support identity not found: {subject}")
        identity.is_active = False
        identity.session_version = int(identity.session_version) + 1
        identity.updated_at = now
        rows = (
            session.query(SupportSessionModel)
            .filter(
                SupportSessionModel.identity_id == identity.identity_id,
                SupportSessionModel.revoked_at.is_(None),
            )
            .all()
        )
        for row in rows:
            row.revoked_at = now
        session.commit()
        return True
