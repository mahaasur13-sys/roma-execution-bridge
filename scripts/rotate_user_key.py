#!/usr/bin/env python3
"""Выпустить новый API-ключ одному пользователю (без HTTP).

Ключ — единственный креденшл тенанта, поэтому механика здесь та же, что в
`/account/rotate-key`: одна транзакция через `db_adapter.rotate_user_api_key`
(users + tenants меняются вместе или не меняются вовсе). Плейнтекст не идёт ни
в stdout, ни в лог: он пишется в файл 0400 в /root/.sealed, а печатается только
путь к нему.

    scripts/rotate_user_key.py --user-id <uuid> [--dry-run]
"""

import argparse
import os
import sys
import time
import uuid

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_adapter as db

SEALED_DIR = "/root/.sealed"


def _new_api_key() -> str:
    return f"roma-{str(uuid.uuid4())[:12]}"


def _write_sealed(user_id: str, key: str) -> str:
    """Записать ключ в файл 0400 и вернуть путь. Плейнтекст больше нигде."""
    os.makedirs(SEALED_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = os.path.join(SEALED_DIR, f"rotated-{user_id[:8]}-{stamp}.txt")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
    try:
        os.write(fd, (key + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o400)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate one user's API key (no HTTP).")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--dry-run", action="store_true", help="nothing is written")
    args = parser.parse_args(argv)

    user = db.get_user_by_id(args.user_id)
    if not user:
        print(f"no such user: {args.user_id}", file=sys.stderr)
        return 2

    tenant_id = user.get("tenant_id") or ""
    if not tenant_id:
        print(f"user {args.user_id} has no tenant", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"dry-run: would rotate user {args.user_id[:8]} tenant={tenant_id}")
        return 0

    new_key = _new_api_key()
    try:
        path = _write_sealed(args.user_id, new_key)
    except OSError as exc:
        print(f"could not write the key file: {exc}", file=sys.stderr)
        return 1

    try:
        db.rotate_user_api_key(args.user_id, tenant_id, new_key)
    except Exception as exc:
        # Ключ не вступил в силу — файл с ним не оставляем.
        os.unlink(path)
        print(f"rotation failed: {exc}", file=sys.stderr)
        return 1

    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
