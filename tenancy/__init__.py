#!/usr/bin/env python3
"""ROMA Multi-Tenant Control Plane — isolation, quotas, RBAC domains, billing."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum

class TenantTier(Enum):
    FREE = "free"; PRO = "pro"; ENTERPRISE = "enterprise"

@dataclass
class TenantQuota:
    max_jobs: int; max_concurrent: int; max_gpu_memory_mb: int; max_storage_gb: int
    rate_limit_jobs_per_hour: int; rate_limit_api_per_minute: int
    gpu_slots: int; priority_weight: int

DEFAULT_QUOTAS = {
    TenantTier.FREE: TenantQuota(100, 2, 4096, 10, 10, 30, 1, 1),
    TenantTier.PRO: TenantQuota(1000, 8, 16384, 100, 100, 120, 4, 4),
    TenantTier.ENTERPRISE: TenantQuota(100000, 64, 65536, 1000, 10000, 1000, 32, 16),
}

@dataclass
class TenantContext:
    tenant_id: str; name: str; tier: TenantTier; quota: TenantQuota
    used_jobs: int = 0; active_jobs: int = 0; used_gpu_mb: int = 0
    rbac_domain: str = "default"; billing_enabled: bool = False
    def can_submit(self) -> bool:
        return self.used_jobs < self.quota.max_jobs and self.active_jobs < self.quota.max_concurrent
    def check_gpu(self, required_mb: int) -> bool:
        return (self.used_gpu_mb + required_mb) <= self.quota.max_gpu_memory_mb
    def check_rate_limit(self, current: int) -> bool:
        return current < self.quota.rate_limit_jobs_per_hour
    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id, "tier": self.tier.value,
            "quota": {"jobs": self.used_jobs, "concurrent": self.active_jobs, "gpu_mb": self.used_gpu_mb},
            "can_submit": self.can_submit(), "rbac_domain": self.rbac_domain,
        }

class TenantIsolationEngine:
    def __init__(self):
        self.tenants: Dict[str, TenantContext] = {}
        self.tenant_gpu_allocation: Dict[str, int] = {}
    def create_tenant(self, tenant_id: str, name: str, tier: TenantTier = TenantTier.FREE) -> TenantContext:
        ctx = TenantContext(tenant_id=tenant_id, name=name, tier=tier, quota=DEFAULT_QUOTAS[tier])
        self.tenants[tenant_id] = ctx
        self.tenant_gpu_allocation[tenant_id] = 0
        return ctx
    def enforce_quota(self, tenant_id: str, required_gpu_mb: int, rate_limiter_hits: int) -> tuple[bool, str]:
        if tenant_id not in self.tenants:
            return False, "TENANT_NOT_FOUND"
        ctx = self.tenants[tenant_id]
        if not ctx.can_submit():
            return False, f"QUOTA_EXCEEDED: jobs {ctx.used_jobs}/{ctx.quota.max_jobs}"
        if not ctx.check_gpu(required_gpu_mb):
            return False, f"GPU_QUOTA_EXCEEDED: {ctx.used_gpu_mb+required_gpu_mb}MB > {ctx.quota.max_gpu_memory_mb}MB"
        if not ctx.check_rate_limit(rate_limiter_hits):
            return False, f"RATE_LIMIT_EXCEEDED: {ctx.quota.rate_limit_jobs_per_hour}/hour"
        return True, "OK"
    def allocate_gpu(self, tenant_id: str, mb: int) -> bool:
        if tenant_id not in self.tenants:
            return False
        self.tenants[tenant_id].used_gpu_mb += mb
        self.tenants[tenant_id].active_jobs += 1
        return True
    def release_gpu(self, tenant_id: str, mb: int):
        if tenant_id not in self.tenants:
            return
        ctx = self.tenants[tenant_id]
        ctx.used_gpu_mb = max(0, ctx.used_gpu_mb - mb)
        ctx.active_jobs = max(0, ctx.active_jobs - 1)

class RBACDomain:
    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id; self.role_bindings: Dict[str, List[str]] = {}
    def grant(self, principal: str, role: str):
        if principal not in self.role_bindings:
            self.role_bindings[principal] = []
        if role not in self.role_bindings[principal]:
            self.role_bindings[principal].append(role)
    def check(self, principal: str, required_roles: List[str]) -> bool:
        if principal not in self.role_bindings:
            return False
        return any(r in self.role_bindings[principal] for r in required_roles)

if __name__ == "__main__":
    engine = TenantIsolationEngine()
    t1 = engine.create_tenant("tenant-asur", "AsurDev", TenantTier.PRO)
    t2 = engine.create_tenant("tenant-alice", "Alice", TenantTier.FREE)
    ok, msg = engine.enforce_quota("tenant-asur", 8192, 5)
    print(f"Enforce PRO quota (8192MB, 5 hits): ok={ok}, msg={msg}")
    ok, msg = engine.enforce_quota("tenant-alice", 8192, 5)
    print(f"Enforce FREE quota (8192MB, 5 hits): ok={ok}, msg={msg}")
    engine.allocate_gpu("tenant-asur", 8192)
    print(f"GPU after alloc: {t1.used_gpu_mb}MB / {t1.quota.max_gpu_memory_mb}MB")
    engine.release_gpu("tenant-asur", 8192)
    print(f"GPU after release: {t1.used_gpu_mb}MB")
    rbac = RBACDomain("tenant-asur")
    rbac.grant("user:dev1", "executor"); rbac.grant("user:dev1", "viewer")
    print(f"RBAC dev1 can submit: {rbac.check('user:dev1', ['executor'])}")
    print(f"RBAC dev1 can delete: {rbac.check('user:dev1', ['admin'])}")
    print(f"\nTenant PRO state: {t1.to_dict()}")
