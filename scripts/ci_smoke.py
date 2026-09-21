#!/usr/bin/env python3
"""ROMA CI Test Suite — Corrected APIs (9/9 passing)"""
import sys; sys.path.insert(0, '.')
import os; os.environ.setdefault('PG_DSN', '')

# Ensure SQLite DB is initialized (required by Cost Gate, Audit, etc.)
import db_adapter as db
db.init_db()

passed = 0; failed = 0
def test(name, fn):
    global passed, failed
    try:
        fn()
        print(f"  PASS: {name}")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {name} -> {e}")
        failed += 1

def t_auth_keys():
    from auth.api_keys import APIKeyManager
    k = APIKeyManager()
    key = k.create_key('o', 'p', permissions=['x'])
    assert key and key.startswith('roma_sk_live_'), f"key gen failed: {key}"

def t_rbac():
    from rbac.engine import RBACEngine, Role
    rbac = RBACEngine()
    rbac.assign_role('u', 'o', Role.DEVELOPER)
    assert rbac.can('u', 'o', 'job:execute'), "rbac failed"

def t_audit():
    from audit.event_log import AuditLog
    a = AuditLog()
    a.log_event('u1', 'job:execute', 'o1', metadata={'j': 't'})
    q = a.query_events(user_id='u1')
    assert len(q) > 0, "audit failed"

def t_cost_gate():
    from cost.gate import EnterpriseDecisionGate
    g = EnterpriseDecisionGate()
    result = g.evaluate(tenant_id='tp', payload={'task': 'train YOLOv8', 'gpu_required': True})
    assert result.result in ('allowed', 'denied'), f"gate: {result}"

def t_billing():
    from billing.pricing_engine import PricingEngine, PricingTier
    pe = PricingEngine()
    calc = pe.calculate(tier=PricingTier.PRO, gpu_s=3600, cpu_s=0, gb_s=86400)
    assert calc.get('total', 0) > 0, "billing failed"

def t_ledger():
    from billing.pg_ledger import PGBillingLedger as BillingLedger
    l = BillingLedger()
    l.append(tenant_id='tp', entry_type='CREDIT', amount=1.0, metadata={})
    bal = l.get_tenant_balance('tp')
    assert bal >= 0, "ledger failed"

def t_gpu_scheduler():
    from scheduler.gpu_scheduler import GPUScheduler
    from queue_manager.queue_manager import QueueManager
    scheduler = GPUScheduler(QueueManager())
    can = scheduler.can_schedule({})
    assert isinstance(can, bool), f"scheduler failed: {can}"

def t_raft():
    from ha.raft_consensus import ROMARaftNode
    n = ROMARaftNode('n1', ['n1','n2'])
    assert n is not None, "raft node failed"

def t_plugin():
    from plugins.plugin_api import PluginCapability
    caps = [c.name for c in PluginCapability]
    assert any('ML' in c or 'GPU' in c for c in caps), f"plugin caps: {caps}"


def t_dispatch_ready():
    from billing.execution_worker import dispatch_is_ready
    assert dispatch_is_ready("local", "queued") is True
    assert dispatch_is_ready("vastai", "queued") is False
    assert dispatch_is_ready("vastai", "running") is True
    assert dispatch_is_ready("vastai", "provisioning") is True

print("=== ROMA CI Tests ===")
test("Auth (API Key Gen)", t_auth_keys)
test("RBAC (Permissions)", t_rbac)
test("Audit (Event Log)", t_audit)
test("Cost Gate (Decision)", t_cost_gate)
test("Billing (Pricing)", t_billing)
test("Ledger (Balance)", t_ledger)
test("GPU Scheduler", t_gpu_scheduler)
test("Raft Consensus", t_raft)
test("Plugin API", t_plugin)
test("Dispatch ready (local queued)", t_dispatch_ready)

print()
print(f"RESULTS: {passed} passed, {failed} failed")
if failed > 0:
    raise SystemExit(f"CI check failed: {failed} failures")
print("CI check passed")
