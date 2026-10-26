"""Денежная политика (spend-cap и overage) — единственный источник истины.

Перенесено из `deps.py` (G-MONEY-CAP-LITERALS), чтобы сверка ledger и
API-путь читали одни и те же числа, а не две копии.
"""

from __future__ import annotations

MONEY_POLICY: dict[str, dict[str, float]] = {
    "free": {"spend_cap_usd": 0.50, "overage_rate": 0.0},
    "start": {"spend_cap_usd": 5.00, "overage_rate": 0.000005},
    "pro": {"spend_cap_usd": 50.00, "overage_rate": 0.000003},
    "enterprise": {"spend_cap_usd": -1.0, "overage_rate": 0.0},
}
