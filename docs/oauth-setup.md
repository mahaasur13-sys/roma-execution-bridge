# ROMA OAuth2 Setup — Google & GitHub Login

## Overview

ROMA supports OAuth2 login via Google and GitHub. When configured, users can sign in with one click — no API key needed.

## Architecture

```
User clicks "Sign in with Google"
       │
       ▼
GET /auth/oauth/login/google  →  Redirect to Google OAuth consent screen
       │
       ▼
Google redirects back to:
GET /auth/oauth/callback/google?code={auth_code}
       │
       ▼
ROMA exchanges code for access token → gets user email/name
       │
       ▼
upsert_oauth_user() → creates tenant + API key if new user
       │
       ▼
create_session() → sets session cookie → redirect to /dashboard
```

## 1. Google OAuth Setup

### Create OAuth 2.0 Client

1. Go to [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials)
2. Click **"Create Credentials"** → **"OAuth client ID"**
3. Application type: **"Web application"**
4. Name: `ROMA Execution Bridge`
5. Authorized redirect URIs:
   ```
   https://roma-execution-bridge-asurdev.zocomputer.io/auth/oauth/callback/google
   ```
6. Click **"Create"** — copy the **Client ID** and **Client Secret**

### Enable APIs

1. Go to [APIs & Services → Library](https://console.cloud.google.com/apis/library)
2. Enable:
   - **Google People API** (for email/profile)

### Configure Consent Screen

1. Go to [OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent)
2. User type: **External** (for public users)
3. Required scopes: `openid`, `email`, `profile`
4. Add test users (your own email) for testing before publishing

## 2. GitHub OAuth Setup

### Create OAuth App

1. Go to [GitHub Developer Settings → OAuth Apps](https://github.com/settings/developers)
2. Click **"New OAuth App"**
3. Fill in:
   - Application name: `ROMA Execution Bridge`
   - Homepage URL: `https://roma-execution-bridge-asurdev.zocomputer.io`
   - Authorization callback URL: `https://roma-execution-bridge-asurdev.zocomputer.io/auth/oauth/callback/github`
4. Click **"Register application"**
5. Copy the **Client ID**
6. Click **"Generate a new client secret"** — copy it

## 3. Environment Variables

Add to `.env` (or Zo Secrets in Settings → Advanced):

```bash
# Google OAuth
GOOGLE_CLIENT_ID=your-google-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-your-google-secret

# GitHub OAuth
GITHUB_CLIENT_ID=Iv1.your-github-client-id
GITHUB_CLIENT_SECRET=your-github-secret

# Redirect base URL (optional — defaults to ROMA service URL)
OAUTH_REDIRECT_BASE=https://roma-execution-bridge-asurdev.zocomputer.io
```

## 4. Verify

```bash
# Health should show oauth enabled
curl https://roma-execution-bridge-asurdev.zocomputer.io/health
# → {"oauth": {"enabled": true, "providers": ["google", "github"]}}

# Visit the login page — OAuth buttons should be active
open https://roma-execution-bridge-asurdev.zocomputer.io/auth/login
```

## 5. User Flow

1. User visits `/auth/login`
2. Clicks **"🔵 Google"** or **"🐙 GitHub"**
3. Redirected to provider's consent screen
4. After authorization, redirected back to ROMA
5. ROMA creates user + tenant (auto-generated API key) if new
6. Session cookie set → dashboard loads automatically

## 6. What Happens on First Login

```python
# Auto-created for each new OAuth user:
tenant_id = "tenant-" + uuid.uuid4().hex[:8]   # e.g., tenant-a1b2c3d4
api_key   = "roma-" + uuid.uuid4().hex[:16]    # e.g., roma-f4e2d9a1b8c71036
# Both stored in 'users' table
# Subscription: free plan (active)
```

## Graceful Degradation

If neither `GOOGLE_CLIENT_ID` nor `GITHUB_CLIENT_ID` is set:
- OAuth buttons on login page are labeled **"Sign in with API-Key only"**
- OAuth redirect endpoints return `503 {"status": "oauth_disabled"}`
- No errors, no broken links

## Troubleshooting

**"Invalid redirect URI" from Google:** Verify the exact URI matches in both Google Console and ROMA — trailing slashes matter.

**"Bad verification code" from GitHub:** The auth code is single-use. Refresh the login page and try again.

**No user created after login:** Check the `users` table — email must be returned by provider. GitHub: ensure `user:email` scope is requested. Google: People API must be enabled.
