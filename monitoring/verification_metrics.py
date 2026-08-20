"""Prometheus metrics for email verification monitoring."""

from prometheus_client import Counter

roma_email_verification_total = Counter(
    "roma_email_verification_total",
    "Email verifications by outcome",
    ["status"],
)

roma_email_send_total = Counter(
    "roma_email_send_total",
    "Verification emails sent (success/fail)",
    ["status"],
)

roma_email_resend_total = Counter(
    "roma_email_resend_total",
    "Verification resend requests",
)

roma_unverified_api_key_blocked_total = Counter(
    "roma_unverified_api_key_blocked_total",
    "API requests blocked — email not verified",
)

roma_verification_token_expired_total = Counter(
    "roma_verification_token_expired_total",
    "Expired verification token attempts",
)

roma_verification_token_invalid_total = Counter(
    "roma_verification_token_invalid_total",
    "Invalid verification token attempts",
)

EMAIL_VERIFY_METRICS = [
    roma_email_verification_total,
    roma_email_send_total,
    roma_email_resend_total,
    roma_unverified_api_key_blocked_total,
    roma_verification_token_expired_total,
    roma_verification_token_invalid_total,
]
