#!/usr/bin/env python3
"""ROMA CI Test Suite — Corrected APIs"""
import sys; sys.path.insert(0, '.')

tests_passed = 0
tests_failed = 0

def test(name, fn):
    global tests_passed, tests_failed
    try:
        fn()
        print(f"  PASS: {name}")
        tests_passed += 1
    except Exception as e:
        print(f"  FAIL: {name} → {e}")
        tests_failed += 1

def assert_true(x, msg): 
    if not x: raise AssertionError(msg)

def t_auth():
    from auth.api_keys import APIKeyManager
    k = APIKeyManager()
    key = k.create_key('o', 'p', permissions=['x'])
    r = k.validate_key(key)
    assert_true(r and r.get('valid'), f"auth failed: {r}")

def t_rbac():
    from rbac.engine import RBACEngine, Role
    rbac = RBACEngine()
    rbac.assign_role('u', 'o', Role.DEVELOPER)
    assert_true(rbac.can('u', 'o', 'job:execute'), "rbac failed")

def t_audit():
    from audit.event_log import AuditLog
    a = AuditLog()
    a.log_event('u1', 'job:execute', 'o1', metadata={'j': 't'})
    q = a.query_events(user_id='u1')
    assert_true(len(q) > 0, f"audit failed: {q}")

def t_cost_gate():
    from cost.gate import DecisionGate
    g = DecisionGate()
    result = g.evaluate(task='train YOLOv8', gpu_required=True, tenant_id='tp', plugin_type='default')
    assert_true(result.get('decision') in ('APPROVED', 'REQUIRES_CONFIRMATION', 'REJECTED'), f"gate: {result}")

def t_billing():
    from billing.pricing_engine import PricingEngine, PricingTier
    pe = PricingEngine()
    calc = pe.calculate(tier=PricingTier.PRO, gpu_s=3600, cpu_s=0, gb_s=86400)
    assert_true(calc.get('total_cost', 0) > 0, f"billing failed: {calc}")

def t_ledger():
    from billing.ledger import BillingLedger
    l = BillingLedger()
    l.append(tenant_id='tp', entry_type='usage', amount=1.0, metadata={})
    bal = l.get_tenant_balance('tp')
    assert_true(bal >= 0, f"ledger failed: {bal}")

def t_gpu_scheduler():
    from scheduler.gpu_scheduler import GPUScheduler
    sched = GPUScheduler()
    can = sched.can_schedule('train YOLOv8', gpu_required=True)
    assert_true(isinstance(can, bool), f"scheduler failed: {can}")

def t_raft():
    from ha.raft_consensus import ROMARaftNode
    n = ROMARaftNode('n1', ['n1','n2'])
    assert_true(n is not None, "raft node failed")

def t_plugin():
    from plugins.plugin_api import PluginCapability
    caps = [c.name for c in PluginCapability]
    assert_true(any('ML' in c or 'GPU' in c or 'TRAINING' in c for c in caps), f"plugin caps: {caps}")

print("=== ROMA CI Tests ===")
test("Auth (API Keys)", t_auth)
test("RBAC (Permissions)", t_rbac)
test("Audit (Event Log)", t_audit)
test("Cost Gate (Decision)", t_cost_gate)
test("Billing (Pricing)", t_billing)
test("Ledger (Balance)", t_ledger)
test("GPU Scheduler", t_gpu_scheduler)
test("Raft Consensus", t_raft)
test("Plugin API", t_plugin)

print()
print(f"RESULTS: {tests_passed} passed, {tests_failed} failed")
sys.exit(0 if tests_failed == 0 else 1)
