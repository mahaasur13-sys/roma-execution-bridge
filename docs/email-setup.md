# SendGrid Email Setup for ROMA

## 1. Create SendGrid Account

1. Go to https://signup.sendgrid.com
2. Choose **Free** plan (100 emails/day)
3. Verify your email address

## 2. Verify Sender Identity

1. Go to **Settings → Sender Authentication** → **Single Sender Verification**
2. Add `beta@roma-execution-bridge.io` (or your domain)
3. Check inbox and click verification link
4. Status should turn **Verified**

## 3. Create API Key

1. Go to **Settings → API Keys** → **Create API Key**
2. Name: `ROMA-Beta-Invitations`
3. Permissions: **Full Access** (or at minimum: `Mail Send`)
4. Copy the generated key (starts with `SG.`)

## 4. Configure ROMA

```bash
# Edit .env file
SENDGRID_API_KEY=SG.your-key-here
FROM_EMAIL=beta@roma-execution-bridge.io
```

## 5. Test SendGrid Integration

```bash
# Dry-run (no keys needed)
python scripts/send_invitations.py --dry-run --limit 3

# With API key (sends real emails)
SENDGRID_API_KEY=SG.your-key python scripts/send_invitations.py --limit 1

# Via admin endpoint
curl -X POST https://roma-execution-bridge-asurdev.zocomputer.io/admin/invite \
  -H "X-API-Key: roma-demo-key-2026" \
  -H "Content-Type: application/json" \
  -d '{"limit": 1, "dry_run": false}'
```

## 6. Configure Webhook (Event Tracking)

To receive delivery/open/click events:

1. Go to **Settings → Mail Settings → Event Webhook**
2. URL: `https://roma-execution-bridge-asurdev.zocomputer.io/webhooks/email`
3. Events to track:
   - Delivered
   - Opened
   - Clicked
   - Bounced
   - Dropped
   - Spam Report
4. Authorization Method: **None** (ROMA accepts unsigned webhooks)
5. Click **Save**

Test webhook from SendGrid UI: **Settings → Mail Settings → Event Webhook → Test Your Integration**

## 7. Monitor

```bash
# Email stats from admin panel
curl https://roma-execution-bridge-asurdev.zocomputer.io/admin/email-stats?api_key=roma-demo-key-2026

# Expected response:
# {"status":"ok","data":{"sent":10,"opened":3,"clicked":2,"bounced":0,"open_rate":30.0,"click_rate":20.0}}
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `SENDGRID_API_KEY not set` | Add key to `.env` or export env var |
| `401 Unauthorized` | Check API key permissions (needs Mail Send) |
| `403 Forbidden` | Verify sender email in SendGrid |
| Webhook not receiving events | Check URL is reachable from internet |
| `database is locked` | SQLite concurrency — wait and retry |
