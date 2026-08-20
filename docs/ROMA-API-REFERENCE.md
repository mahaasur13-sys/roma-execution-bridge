# ROMA Execution Bridge v1.2.0 — API Reference

**Base URL:** https://roma-execution-bridge-asurdev.zocomputer.io
**Local:** http://localhost:8900
**OpenAPI:** 3.1.0 | **Endpoints:** 34
**Docs:** /docs (Swagger UI) | /redoc (ReDoc)
**Auth:** API key (X-API-Key header) + OAuth2 (Google/GitHub)

## Uncategorized

### `GET /admin`

**Admin Page**
> Admin dashboard HTML page.

- **200**: Successful Response

### `GET /admin/analytics`

**Admin Analytics**
> Get analytics overview (JSON).

- **200**: Successful Response

### `GET /admin/analytics/events`

**Admin Analytics Events**
> Get paginated event list.

- **200**: Successful Response

### `GET /admin/analytics/users`

**Admin Analytics Users**
> Get user list for analytics.

- **200**: Successful Response

### `GET /admin/email-stats`

**Admin Email Stats**
> Get email sending statistics.

- **200**: Successful Response

### `GET /admin/feedback`

**Admin Feedback**
> Get feedback list (JSON).

- **200**: Successful Response

### `POST /admin/invite`

**Admin Invite**
> Send beta invitations. Dry-run if no SendGrid API key.

- **200**: Successful Response

### `GET /auth/login`

**Login Page**
> Show login form.

- **200**: Successful Response

### `POST /auth/login`

**Login**
> Process login form submission.

- **200**: Successful Response

### `GET /auth/logout`

**Logout**
> Clear session and redirect to login.

- **200**: Successful Response

### `GET /auth/oauth/callback/{provider}`

**Oauth Callback**
> Handle OAuth callback — exchange code, create/update user, start session.

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `provider` | path | string | Yes |  |
| `code` | query | string | No |  |
| `error` | query | string | No |  |

- **200**: Successful Response
- **422**: Validation Error

### `GET /auth/oauth/login/{provider}`

**Oauth Login**
> Redirect to Google or GitHub OAuth authorization page.

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `provider` | path | string | Yes |  |

- **200**: Successful Response
- **422**: Validation Error

### `GET /beta`

**Beta Page**

- **200**: Successful Response

### `POST /beta/apply`

**Beta Apply**

**Body:** JSON (`Payload`)

- **200**: Successful Response
- **422**: Validation Error

### `GET /beta/leads`

**Beta Leads**

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `x-api-key` | header | string | No |  |

- **200**: Successful Response
- **422**: Validation Error

### `POST /billing/create-checkout-session`

**Create Checkout Session**
> Создаёт платёжную ссылку CloudPayments (hosted page).
Возвращает {"url": "https://..."}.

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `x-api-key` | header | string | No |  |

**Body:** JSON (`#/components/schemas/CheckoutRequest`)

- **200**: Successful Response
- **422**: Validation Error

### `POST /cancel/{job_id}`

**Cancel Job**

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `job_id` | path | string | Yes |  |
| `x-api-key` | header | string | No |  |

- **200**: Successful Response
- **422**: Validation Error

### `GET /dashboard`

**Dashboard**

- **200**: Successful Response

### `POST /demo/{demo_name}`

**Run Demo**

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `demo_name` | path | string | Yes |  |
| `x-api-key` | header | string | No |  |

- **200**: Successful Response
- **422**: Validation Error

### `POST /feedback`

**Submit Feedback**
> Submit user feedback. Public endpoint, no auth required.

- **200**: Successful Response

### `GET /health`

**Health**

- **200**: Successful Response

### `GET /jobs`

**List Jobs**

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `x-api-key` | header | string | No |  |

- **200**: Successful Response
- **422**: Validation Error

### `GET /metrics`

**Metrics**

- **200**: Successful Response

### `POST /slurm/cancel/{slurm_job_id}`

**Slurm Cancel**
> Cancel Slurm job via scancel.

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `slurm_job_id` | path | string | Yes |  |
| `x-api-key` | header | string | No |  |

- **200**: Successful Response
- **422**: Validation Error

### `GET /slurm/status/{slurm_job_id}`

**Slurm Status**
> Get Slurm job status via sacct/squeue.

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `slurm_job_id` | path | string | Yes |  |
| `x-api-key` | header | string | No |  |

- **200**: Successful Response
- **422**: Validation Error

### `GET /stats/daily`

**Daily Stats**

- **200**: Successful Response

### `GET /status/{job_id}`

**Get Status**

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `job_id` | path | string | Yes |  |
| `x-api-key` | header | string | No |  |

- **200**: Successful Response
- **422**: Validation Error

### `POST /submit`

**Submit Task**

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `x-api-key` | header | string | No |  |

**Body:** JSON (`#/components/schemas/RomaTaskInput`)

- **202**: Successful Response
- **422**: Validation Error

### `POST /submit/cluster`

**Submit Atom Cluster**

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `x-api-key` | header | string | No |  |

**Body:** JSON (`Payload`)

- **202**: Successful Response
- **422**: Validation Error

### `GET /usage`

**Get Usage**

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `x-api-key` | header | string | No |  |

- **200**: Successful Response
- **422**: Validation Error

### `POST /webhooks/cloudpayments`

**Cloudpayments Webhook**
> Handle CloudPayments webhook notifications.
Verifies Content-HMAC signature and updates tenant subscription.

Events handled: Pay, Recurrent, Fail, Cancel, Unsubscribe.

- **200**: Successful Response

### `POST /webhooks/email`

**Sendgrid Webhook**
> Receive SendGrid event notifications.
Events: delivered, open, click, bounce, dropped, spamreport.
See: https://docs.sendgrid.com/for-developers/tracking-events/event

- **200**: Successful Response

### `GET /workers`

**List Workers**

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `x-api-key` | header | string | No |  |

- **200**: Successful Response
- **422**: Validation Error

### `GET /workers/{worker_id}`

**Get Worker**

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `worker_id` | path | string | Yes |  |
| `x-api-key` | header | string | No |  |

- **200**: Successful Response
- **422**: Validation Error

### `POST /workers/{worker_id}/drain`

**Drain Worker**

| Param | In | Type | Required | Description |
|-------|----|------|----------|-------------|
| `worker_id` | path | string | Yes |  |
| `x-api-key` | header | string | No |  |

- **200**: Successful Response
- **422**: Validation Error

---
