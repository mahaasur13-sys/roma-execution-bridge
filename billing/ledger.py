#!/usr/bin/env python3
"""ROMA Billing Ledger — append-only ledger of all billing state changes."""
from typing import Optional
import time, json

class BillingLedger:
    """Append-only ledger — every billing event is recorded, never mutated."""
    def __init__(self):
        self._entries: list[dict] = []

    def append(self, tenant_id: str, entry_type: str, amount: float, currency: str = "USD", metadata: dict = None) -> None:
        entry = {
            "ledger_id": f"led-{len(self._entries) + 1:06d}",
            "timestamp": time.time(),
            "tenant_id": tenant_id,
            "type": entry_type,
            "amount": amount,
            "currency": currency,
            "metadata": metadata or {},
        }
        self._entries.append(entry)

    def credit(self, tenant_id: str, amount: float, currency: str = "USD", **meta) -> None:
        self.append(tenant_id, "CREDIT", amount, currency, meta)

    def debit(self, tenant_id: str, amount: float, currency: str = "USD", **meta) -> None:
        self.append(tenant_id, "DEBIT", amount, currency, meta)

    def get_tenant_balance(self, tenant_id: str) -> float:
        balance = 0.0
        for e in self._entries:
            if e["tenant_id"] == tenant_id:
                if e["type"] == "CREDIT":
                    balance += e["amount"]
                elif e["type"] == "DEBIT":
                    balance -= e["amount"]
        return balance

    def get_tenant_entries(self, tenant_id: str) -> list[dict]:
        return [e for e in self._entries if e["tenant_id"] == tenant_id]

    def ledger_summary(self) -> dict:
        by_tenant = {}
        for e in self._entries:
            t = e["tenant_id"]
            if t not in by_tenant:
                by_tenant[t] = {"credits": 0.0, "debits": 0.0, "net": 0.0, "entries": 0}
            by_tenant[t]["entries"] += 1
            if e["type"] == "CREDIT":
                by_tenant[t]["credits"] += e["amount"]
            elif e["type"] == "DEBIT":
                by_tenant[t]["debits"] += e["amount"]
        for t in by_tenant:
            by_tenant[t]["net"] = by_tenant[t]["credits"] - by_tenant[t]["debits"]
        return by_tenant

def simulate_ledger() -> None:
    ledger = BillingLedger()
    ledger.credit("tenant-abc", 100.0, metadata={"source": "subscription", "plan": "PRO"})
    ledger.credit("tenant-abc", 10.0, metadata={"source": "credit_topup"})
    ledger.debit("tenant-abc", 3.20, metadata={"description": "GPU usage 2h @ $0.0016/sec"})
    ledger.debit("tenant-abc", 0.60, metadata={"description": "plugin execution 1 unit"})
    ledger.debit("tenant-xyz", 1.00, metadata={"description": "FREE tier GPU usage"})
    print("Tenant ABC balance:", f"${ledger.get_tenant_balance('tenant-abc'):.2f}")
    print("Tenant XYZ balance:", f"${ledger.get_tenant_balance('tenant-xyz'):.2f}")
    summary = ledger.ledger_summary()
    print("Summary:", json.dumps(summary, indent=2))

if __name__ == "__main__":
    simulate_ledger()