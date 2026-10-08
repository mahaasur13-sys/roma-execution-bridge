#!/usr/bin/env python3
"""Offline provisioning for H2a support identities (operator-run, no HTTP surface).

Usage:
    python scripts/support_identity_admin.py create --subject agent@example \\
        --tenant-id acme --tenant-id globex --expires-in-days 90
    python scripts/support_identity_admin.py revoke-credential --credential-id <uuid>
    python scripts/support_identity_admin.py revoke-sessions --subject agent@example
    python scripts/support_identity_admin.py deactivate --subject agent@example

The credential is printed once, after a committed transaction; only its SHA-256
digest is stored. Nothing here talks to the network or to an admin endpoint.
Without `--db-url` the standard support DB resolution applies (PG_DSN, and
fail-closed in production).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support_chat import identity_admin
from support_chat.identity import SupportAuthError


def _factory(db_url: str | None):
    from support_chat.db import get_session_factory

    factory = get_session_factory(db_url)
    if db_url and db_url.startswith("sqlite"):
        from support_chat.db_models import Base

        with factory() as session:
            Base.metadata.create_all(session.get_bind())
    return factory


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=None, help="explicit DB URL (tests/local)")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create identity + memberships + credential")
    create.add_argument("--subject", required=True)
    create.add_argument("--tenant-id", action="append", default=[], required=True)
    create.add_argument("--display-name", default=None)
    create.add_argument("--expires-in-days", type=int, default=None)
    create.add_argument("--expires-at", default=None)

    revoke_cred = sub.add_parser("revoke-credential", help="revoke credential(s)")
    revoke_cred.add_argument("--credential-id", default=None)
    revoke_cred.add_argument("--subject", default=None)

    revoke_sessions = sub.add_parser("revoke-sessions", help="invalidate all sessions")
    revoke_sessions.add_argument("--subject", required=True)

    deactivate = sub.add_parser("deactivate", help="deactivate an identity")
    deactivate.add_argument("--subject", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        factory = _factory(args.db_url)
        if args.command == "create":
            result = identity_admin.create_identity(
                factory,
                subject=args.subject,
                tenant_ids=args.tenant_id,
                display_name=args.display_name,
                expires_in_days=args.expires_in_days,
                expires_at=args.expires_at,
            )
            print(f"identity_id={result['identity_id']}")
            print(f"subject={result['subject']}")
            print(f"tenant_ids={','.join(result['tenant_ids'])}")
            print(f"credential_id={result['credential_id']}")
            print(f"credential_expires_at={result['credential_expires_at']}")
            print("credential (shown once, store in a secret manager):")
            print(result["credential"])
            return 0
        if args.command == "revoke-credential":
            count = identity_admin.revoke_credential(
                factory, credential_id=args.credential_id, subject=args.subject
            )
            print(f"revoked_credentials={count}")
            return 0
        if args.command == "revoke-sessions":
            result = identity_admin.revoke_sessions(factory, subject=args.subject)
            print(f"session_version={result['session_version']}")
            print(f"revoked_sessions={result['revoked_sessions']}")
            return 0
        if args.command == "deactivate":
            identity_admin.deactivate_identity(factory, subject=args.subject)
            print("identity_deactivated=1")
            return 0
    except SupportAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("error: unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
