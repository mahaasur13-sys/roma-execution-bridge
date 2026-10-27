-- 017_users_api_key_hash.sql — SEC: users.api_key_hash for hash-only key lookup
--
-- Step 1 of the users-key migration: add the hash column, backfill it from the
-- plaintext key and make it the lookup path (partial UNIQUE).
--
-- The plaintext users.api_key is NOT cleared here. It is zeroed only after every
-- writer (create_user_with_password / create_email_user / upsert_oauth_user)
-- fills api_key_hash — that is 018.
--
-- Idempotent: a re-run adds no column, backfills no row and re-creates no index,
-- so both run_migrations.py passes and a manual re-apply are safe.

ALTER TABLE users ADD COLUMN IF NOT EXISTS api_key_hash TEXT;

UPDATE users
   SET api_key_hash = encode(sha256(convert_to(api_key, 'UTF8')), 'hex')
 WHERE coalesce(api_key, '') <> ''
   AND coalesce(api_key_hash, '') = '';

CREATE UNIQUE INDEX IF NOT EXISTS users_api_key_hash_uidx
  ON users (api_key_hash)
  WHERE api_key_hash IS NOT NULL AND api_key_hash <> '';
