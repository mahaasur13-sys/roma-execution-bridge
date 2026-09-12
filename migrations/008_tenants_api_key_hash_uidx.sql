-- Unique index on tenants.api_key_hash. Does not drop plaintext api_key.
-- Already applied on live ROMA :5433 (2026-09-12). IF NOT EXISTS = idempotent.

CREATE UNIQUE INDEX IF NOT EXISTS tenants_api_key_hash_uidx
  ON tenants (api_key_hash)
  WHERE api_key_hash IS NOT NULL AND api_key_hash <> '';
