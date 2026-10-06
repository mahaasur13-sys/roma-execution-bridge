ALTER TABLE tenants ADD COLUMN IF NOT EXISTS api_key_hash TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS tenants_api_key_hash_uidx
  ON tenants (api_key_hash)
  WHERE api_key_hash IS NOT NULL AND api_key_hash <> '';
