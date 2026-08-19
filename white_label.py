"""DecisionOS — White-label branding per tenant (Week 4)."""
import db_adapter as db
from typing import Optional

DEFAULT_BRANDING = {
    "product_title": "DecisionOS",
    "logo_url": "/static/logo.png",
    "theme_color": "#0a1628",
    "accent_color": "#00d4aa",
    "company_name": "DecisionOS",
    "support_email": "support@decisionos.io",
    "footer_text": "Powered by DecisionOS",
}

# Machine-readable deny reasons
DENY_REASONS = {
    "quota_exceeded": "Monthly job limit reached. Upgrade your plan.",
    "cost_over_budget": "Estimated cost exceeds your budget limit.",
    "policy_violation": "Action violates your tenant policy.",
    "transition_denied": "Invalid status transition.",
    "tool_not_allowed": "This tool is not available on your plan.",
    "tenant_not_found": "Tenant not found or invalid API key.",
}


def get_branding(tenant_id: str) -> dict:
    """Resolve branding for tenant, falling back to defaults."""
    tenant = db.get_tenant(tenant_id)
    if not tenant:
        return DEFAULT_BRANDING
    tier = tenant.get("tier", "start")
    tier_profile = db.get_tier_profile(tier)
    if not tier_profile:
        return DEFAULT_BRANDING

    branding = dict(DEFAULT_BRANDING)
    if tier_profile.get("white_label"):
        branding["product_title"] = tenant.get("company_name") or tenant.get("name") or DEFAULT_BRANDING["product_title"]
        branding["company_name"] = tenant.get("company_name") or tenant.get("name") or DEFAULT_BRANDING["company_name"]
        branding["logo_url"] = tenant.get("logo_url") or DEFAULT_BRANDING["logo_url"]
        branding["theme_color"] = tenant.get("theme_color") or DEFAULT_BRANDING["theme_color"]
        branding["footer_text"] = f"Powered by {branding['company_name']}"
    return branding


def get_deny_reason(code: str) -> str:
    """Human-readable deny reason with fallback."""
    return DENY_REASONS.get(code, f"Denied: {code}")
