"""Route-contract guard for the main.py split (A1).

Freezes the complete (method, path) surface of the ROMA app — including routes
mounted via ``app.include_router`` — so any future refactor of ``main.py`` into
``APIRouter`` modules can never silently change a URL path, HTTP method, or drop
a route.

If this test fails after a refactor, the route surface diverged from the
snapshot captured at the start of the split. Update the snapshot ONLY when the
change is intentional and backward-compatible (never rename/remove public paths).
"""

from __future__ import annotations

import main


def _route_surface(app) -> set[tuple[str, str]]:
    surface: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or []
        for method in methods:
            surface.add((method, route.path))
    return surface


# Snapshot captured before the A1 extraction (models -> models/app.py,
# webhooks -> routers/webhooks.py, deps -> deps.py, admin -> routers/admin.py).
# Identical surface verified after the split.
EXPECTED_ROUTES: set[tuple[str, str]] = {
    ("GET", "/"),
    ("GET", "/admin"),
    ("GET", "/admin/analytics"),
    ("GET", "/admin/analytics/events"),
    ("GET", "/admin/analytics/users"),
    ("GET", "/admin/backends"),
    ("GET", "/admin/email-stats"),
    ("GET", "/admin/feedback"),
    ("GET", "/admin/invites"),
    ("GET", "/admin/verification-stats"),
    ("GET", "/api/beta/status"),
    ("GET", "/api/beta/validate-invite"),
    ("GET", "/auth/login"),
    ("GET", "/auth/logout"),
    ("GET", "/auth/oauth/callback/{provider}"),
    ("GET", "/auth/oauth/login/{provider}"),
    ("GET", "/auth/verify-email"),
    ("GET", "/beta"),
    ("GET", "/beta/leads"),
    ("GET", "/billing/balance"),
    ("GET", "/billing/ledger"),
    ("GET", "/billing/spend-cap"),
    ("GET", "/dashboard"),
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/health"),
    ("GET", "/jobs"),
    ("GET", "/metrics"),
    ("GET", "/openapi.json"),
    ("GET", "/ping"),
    ("GET", "/redoc"),
    ("GET", "/slurm/status/{slurm_job_id}"),
    ("GET", "/stats/daily"),
    ("GET", "/status/{job_id}"),
    ("GET", "/usage"),
    ("GET", "/v1/crypto/invoices/{invoice_id}"),
    ("GET", "/v1/decisions"),
    ("GET", "/v1/decisions/{decision_id}"),
    ("GET", "/v1/support/tickets"),
    ("GET", "/v1/support/tickets/{ticket_id}"),
    ("GET", "/workers"),
    ("GET", "/workers/{worker_id}"),
    ("HEAD", "/docs"),
    ("HEAD", "/docs/oauth2-redirect"),
    ("HEAD", "/openapi.json"),
    ("HEAD", "/redoc"),
    ("POST", "/admin/invite"),
    ("POST", "/admin/invites/create"),
    ("POST", "/admin/invites/deactivate"),
    ("POST", "/admin/test-alert"),
    ("POST", "/api/chat/stream"),
    ("POST", "/auth/login"),
    ("POST", "/auth/resend-verification"),
    ("POST", "/auth/signup"),
    ("POST", "/beta/apply"),
    ("POST", "/billing/create-checkout-session"),
    ("POST", "/billing/top-up"),
    ("POST", "/cancel/{job_id}"),
    ("POST", "/complete/{job_id}"),
    ("POST", "/demo/{demo_name}"),
    ("POST", "/feedback"),
    ("POST", "/slurm/cancel/{slurm_job_id}"),
    ("POST", "/submit"),
    ("POST", "/submit/cluster"),
    ("POST", "/v1/crypto/invoices"),
    ("POST", "/v1/crypto/wallets"),
    ("POST", "/v1/crypto/wallets/{wallet_id}/addresses"),
    ("POST", "/v1/crypto/wallets/{wallet_id}/monero/subaddress"),
    ("POST", "/v1/crypto/wallets/{wallet_id}/rotate"),
    ("POST", "/v1/crypto/webhooks/nowpayments"),
    ("POST", "/v1/decisions"),
    ("POST", "/v1/decisions/evaluate"),
    ("POST", "/v1/jobs/{job_id}/cancel"),
    ("POST", "/v1/jobs/{job_id}/complete"),
    ("POST", "/v1/jobs/{job_id}/retry"),
    ("POST", "/v1/jobs/{job_id}/worker-ack"),
    ("POST", "/v1/support/tickets"),
    ("POST", "/v1/support/tickets/{ticket_id}/assign"),
    ("POST", "/v1/support/tickets/{ticket_id}/csat"),
    ("POST", "/v1/support/tickets/{ticket_id}/messages"),
    ("POST", "/v1/support/tickets/{ticket_id}/transition"),
    ("POST", "/webhooks/cloudpayments"),
    ("POST", "/webhooks/email"),
    ("POST", "/workers/{worker_id}/drain"),
}


def test_route_surface_unchanged_after_split():
    actual = _route_surface(main.app)
    assert actual == EXPECTED_ROUTES, (
        "Route surface diverged from the A1 snapshot.\n"
        f"  missing: {sorted(EXPECTED_ROUTES - actual)}\n"
        f"  extra:   {sorted(actual - EXPECTED_ROUTES)}"
    )


def test_admin_routes_mounted_on_same_paths():
    """The 13 admin routes moved to routers/admin.py but stay on the same paths."""
    actual = _route_surface(main.app)
    admin_routes = {
        ("GET", "/admin"),
        ("GET", "/admin/analytics"),
        ("GET", "/admin/analytics/events"),
        ("GET", "/admin/analytics/users"),
        ("GET", "/admin/backends"),
        ("GET", "/admin/email-stats"),
        ("GET", "/admin/feedback"),
        ("GET", "/admin/invites"),
        ("GET", "/admin/verification-stats"),
        ("POST", "/admin/invite"),
        ("POST", "/admin/invites/create"),
        ("POST", "/admin/invites/deactivate"),
        ("POST", "/admin/test-alert"),
    }
    assert admin_routes <= actual


def test_auth_routes_mounted_on_same_paths():
    """The 7 auth paths moved to routers/auth.py but stay on the same paths."""
    actual = _route_surface(main.app)
    auth_routes = {
        ("GET", "/auth/login"),
        ("POST", "/auth/login"),
        ("GET", "/auth/logout"),
        ("POST", "/auth/signup"),
        ("GET", "/auth/verify-email"),
        ("POST", "/auth/resend-verification"),
        ("GET", "/auth/oauth/login/{provider}"),
        ("GET", "/auth/oauth/callback/{provider}"),
    }
    assert auth_routes <= actual


def test_billing_routes_mounted_on_same_paths():
    """The 5 billing paths moved to routers/billing.py but stay on the same paths."""
    actual = _route_surface(main.app)
    billing_routes = {
        ("POST", "/billing/top-up"),
        ("POST", "/billing/create-checkout-session"),
        ("GET", "/billing/ledger"),
        ("GET", "/billing/spend-cap"),
        ("GET", "/billing/balance"),
    }
    assert billing_routes <= actual


def test_beta_routes_mounted_on_same_paths():
    """The 5 beta paths moved to routers/beta.py but stay on the same paths."""
    actual = _route_surface(main.app)
    beta_routes = {
        ("GET", "/beta"),
        ("POST", "/beta/apply"),
        ("GET", "/beta/leads"),
        ("GET", "/api/beta/status"),
        ("GET", "/api/beta/validate-invite"),
    }
    assert beta_routes <= actual


def test_extracted_webhooks_router_is_mounted():
    """The webhook routes moved to routers/webhooks.py but stay on the same paths."""
    actual = _route_surface(main.app)
    assert ("POST", "/webhooks/cloudpayments") in actual
    assert ("POST", "/webhooks/email") in actual


def test_models_importable_from_models_app():
    """Models were extracted to models/app.py without changing validation."""
    from models.app import ChatMessage, RomaTaskInput

    task = RomaTaskInput(task="hello")
    assert task.priority == 5

    msg = ChatMessage(role="user", content="hi")
    assert msg.role == "user"
