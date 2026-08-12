# ROMA Admin Dashboard

## Access

```
https://roma-execution-bridge-asurdev.zocomputer.io/admin/dashboard?api_key=roma-demo-key-2026
```

Requires a valid API key (any tenant). In production, restrict to admin-only.

## Sections

| Section | Description | Data Source |
|---------|-------------|-------------|
| **Total Users** | Unique tenants with events | `user_events` table |
| **Active Today** | Distinct tenants active in last 24h | `user_events` query |
| **Login→Submit** | Conversion: % of users who submitted a job after login | Computed from events |
| **Total Jobs** | All job_submit + job_complete + job_failed events | `user_events` aggregation |
| **Activity Timeline** | Line chart: logins, submits, completes over time | `/admin/analytics/overview` |
| **Jobs by Backend** | Donut chart: breakdown by local/slurm/ray/worker | `/admin/analytics/overview` |
| **Users Table** | All tenants sorted by last seen, searchable | `/admin/analytics/users` |
| **Recent Events** | Raw event log with type filter | `/admin/analytics/events` |

## Admin Analytics API

### GET /admin/analytics/overview
Aggregated stats for the last 30 days.

```json
{
  "total_users": 25,
  "active_users_today": 5,
  "conversion": {"login_to_submit": 0.72, "submit_to_complete": 0.85},
  "jobs": {"total": 342, "by_backend": {"local": 300, "slurm": 30}},
  "events_timeline": {"dates": [...], "logins": [...], "submits": [...], "completes": [...]}
}
```

### GET /admin/analytics/users
List all tenants with activity stats. Supports `start_date`, `end_date`, `sort_by` params.

### GET /admin/analytics/events
Raw events with pagination. Supports `limit`, `offset`, `event_type`, `tenant_id`, `from_date`, `to_date`.

## Tracked Events

| Event | Trigger |
|-------|---------|
| `login` | Successful authentication (session, OAuth, API key) |
| `logout` | POST /auth/logout |
| `dashboard_view` | GET /dashboard (valid session) |
| `job_submit` | POST /submit success |
| `job_complete` | Job status update → completed |
| `job_failed` | Job status update → failed |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ANALYTICS_ENABLED` | true | Master switch for event logging |
