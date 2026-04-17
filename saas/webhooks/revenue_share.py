"""RevenueShareCalculator — tiered 10-20% per tenant."""
import time
TIER_THRESHOLDS = [(1000_00, 0.10), (5000_00, 0.15), (float("inf"), 0.20)]
class RevenueShareCalculator:
    def __init__(self, ledger=None):
        self.ledger = ledger
    def get_rate(self, monthly_revenue_cents: int) -> float:
        for threshold, rate in TIER_THRESHOLDS:
            if monthly_revenue_cents < threshold:
                return rate
        return 0.20
    def calculate(self, tenant_id: str, gross_cents: int) -> dict:
        rate = 0.10
        if self.ledger:
            mr = self.ledger.get_monthly_revenue(tenant_id)
            rate = self.get_rate(int(mr * 100))
        deduction = round(gross_cents * rate)
        return {
            "gross_amount_cents": gross_cents,
            "revenue_share_percent": rate,
            "revenue_share_cents": deduction,
            "net_to_platform_cents": gross_cents - deduction,
            "tenant_id": tenant_id, "calculated_at": int(time.time())
        }
