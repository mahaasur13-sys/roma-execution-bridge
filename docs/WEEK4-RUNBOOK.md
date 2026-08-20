# DecisionOS — Week 4 Runbook

## 1. Migration

```bash
# Apply schema
su - postgres -c "psql -d astrofin -f migrations/001_decisionos_schema.sql"

# Enable PG backend
export PG_DSN="postgresql://postgres:postgres@localhost:5432/astrofin"
uvicorn main:app --host 0.0.0.0 --port 8900
```

## 2. Tenant + API Key

```bash
# Seed tenant
python3 -c "
import hashlib, psycopg2
h = hashlib.sha256(b'YOUR-API-KEY').hexdigest()
conn = psycopg2.connect('postgresql://postgres:postgres@localhost:5432/astrofin')
conn.cursor().execute(
    'INSERT INTO tenants (tenant_id, name, api_key_hash, tier) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING',
    ('your-tenant', 'Your Corp', h, 'start')
)
conn.commit()
"
```

## 3. Verify Gate

```bash
# Policy engine
python3 -c "from policy.engine import evaluate_policies; print(evaluate_policies('your-tenant','job.submit',{}))"

# Transition guard
python3 -c "from policy.transition import validate_transition; print(validate_transition('queued','running'))"
```

## 4. Verify Audit

```sql
SELECT event_type, tenant_id, data->>'reason' as reason, created_at
FROM decisionos_audit_events ORDER BY created_at DESC LIMIT 10;
```

## 5. Rollback

```bash
# Stop service
kill $(lsof -ti :8900)

# Revert to previous commit
git reset --hard <prev-commit>

# Remove PG_DSN → fallback to SQLite
unset PG_DSN

# Verify
curl -s http://localhost:8900/health
```

## 6. Tier Profiles

| Tier | Jobs/mo | GPU/mo | Concurrent | White Label | AI Tools |
|------|---------|--------|------------|-------------|----------|
| Start | 50 | 10h | 5 | No | Read-only |
| Pro | 150 | 50h | 20 | No | Scoped |
| Enterprise | ∞ | ∞ | 100 | Yes | Full |

---

**Version:** DecisionOS v1.0.0  |  **Author:** Felix (@asurdev)
