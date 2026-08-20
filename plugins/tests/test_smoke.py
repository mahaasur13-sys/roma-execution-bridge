"""DecisionOS v1.0 Plugin Architecture — 15 smoke tests."""
import sys
sys.path.insert(0, "/home/workspace/roma-execution-bridge")

from plugins.domain.plugin import (
    PluginManifest, PluginInstance, PluginState, PluginCategory,
    PluginTier, ThoughtStep, ThoughtTrace, PluginMarketplaceItem,
)
from plugins.core.manager import PluginManager
from plugins.core.manager import PluginError, PluginNotFoundError, PluginSandboxViolation

def _m(name, cat=PluginCategory.POLICY):
    return PluginManifest(name=name, version="1.0.0", display_name=name,
                          category=cat, entry_point="pkg.mod:Cls")

# ── 1: Manifest creation ──
def test_manifest_creation():
    m = PluginManifest(name="test-plugin", version="1.0.0", display_name="Test",
                       category=PluginCategory.POLICY, entry_point="x.y:Z")
    assert m.name == "test-plugin"
    assert m.version == "1.0.0"

# ── 2: Manifest validation ──
def test_manifest_validation():
    try:
        PluginManifest(name="", version="a.b.c", display_name="X",
                       category=PluginCategory.POLICY, entry_point="x.y:Z")
        assert False, "Should raise"
    except Exception:
        pass

# ── 3: Manifest roundtrip ──
def test_manifest_roundtrip():
    m = PluginManifest(name="demo", version="2.0.0", display_name="Demo",
                       category=PluginCategory.POLICY, entry_point="x.y:Z",
                       minimum_tier=PluginTier.PRO)
    js = m.model_dump_json()
    m2 = PluginManifest.model_validate_json(js)
    assert m2.name == "demo"
    assert m2.minimum_tier == PluginTier.PRO

# ── 4: PluginInstance creation ──
def test_instance_creation():
    m = _m("p")
    inst = PluginInstance(manifest=m, state=PluginState.LOADED)
    assert inst.state == PluginState.LOADED
    assert inst.manifest.name == "p"

# ── 5: Manager lifecycle ──
def test_manager_lifecycle():
    mgr = PluginManager()
    m = _m("test-lifecycle")
    mgr._registry["test-lifecycle"] = PluginInstance(manifest=m, state=PluginState.LOADED)
    assert mgr.get("test-lifecycle").state == PluginState.LOADED

# ── 6: Missing plugin ──
def test_get_missing():
    mgr = PluginManager()
    try:
        mgr.get("nonexistent")
        assert False
    except PluginNotFoundError:
        pass

# ── 7: Sandbox violation ──
def test_sandbox_violation():
    err = PluginSandboxViolation("access denied")
    assert "access denied" in str(err)
    assert isinstance(err, PluginError)

# ── 8: ThoughtStep ──
def test_thought_step():
    s = ThoughtStep(agent="PE", thought="Checking policy for tenant", data={"r": 1}, confidence=0.9)
    assert s.agent == "PE"
    assert s.confidence == 0.9
    assert "Checking policy" in s.thought

# ── 9: ThoughtTrace ──
def test_thought_trace():
    t = ThoughtTrace(
        plugin_name="test-policy",
        session_id="s1",
        steps=(
            ThoughtStep(agent="A", thought="step 1", data={}, confidence=1.0),
            ThoughtStep(agent="B", thought="step 2", data={}, confidence=0.8),
        ),
    )
    assert len(t.steps) == 2
    assert t.plugin_name == "test-policy"

# ── 10: Manager trace ──
def test_manager_trace():
    mgr = PluginManager()
    tid = mgr.start_trace("test-plugin", "session-1")
    assert tid
    mgr.add_thought(tid, thought="Processing request", data={"r": 1}, confidence=0.95)
    trace = mgr.finish_trace(tid, {"final": True})
    assert len(trace.steps) == 1
    assert trace.steps[0].agent == "test-plugin"

# ── 11: Manager list_all ──
def test_manager_list_all():
    mgr = PluginManager()
    mgr._registry["a1"] = PluginInstance(manifest=_m("a1"), state=PluginState.LOADED)
    mgr._registry["a2"] = PluginInstance(manifest=_m("a2", PluginCategory.CRYPTO), state=PluginState.LOADED)
    assert len(mgr.list_all()) == 2

# ── 12: Manager list_by_category ──
def test_manager_list_by_category():
    mgr = PluginManager()
    mgr._registry["c1"] = PluginInstance(manifest=_m("c1", PluginCategory.POLICY), state=PluginState.LOADED)
    mgr._registry["c2"] = PluginInstance(manifest=_m("c2", PluginCategory.CRYPTO), state=PluginState.LOADED)
    assert len(mgr.list_by_category(PluginCategory.POLICY)) == 1

# ── 13: Manager exists ──
def test_manager_exists():
    mgr = PluginManager()
    mgr._registry["exists-test"] = PluginInstance(manifest=_m("exists-test"), state=PluginState.LOADED)
    assert mgr.exists("exists-test")
    assert not mgr.exists("nope")

# ── 14: Marketplace item ──
def test_marketplace_item():
    m = _m("market-plugin")
    item = PluginMarketplaceItem(slug="cool-plugin", manifest=m, downloads=500, rating=4.2)
    assert item.rating == 4.2
    assert item.slug == "cool-plugin"
    js = item.model_dump_json()
    i2 = PluginMarketplaceItem.model_validate_json(js)
    assert i2.slug == "cool-plugin"

# ── 15: Tier visibility ──
def test_tier_visibility():
    m_free = PluginManifest(name="f", version="1.0.0", display_name="F",
                            category=PluginCategory.POLICY, entry_point="x.y:Z",
                            minimum_tier=PluginTier.FREE)
    m_pro = PluginManifest(name="p", version="1.0.0", display_name="P",
                           category=PluginCategory.POLICY, entry_point="x.y:Z",
                           minimum_tier=PluginTier.PRO)
    i_free = PluginInstance(manifest=m_free, state=PluginState.LOADED)
    i_pro = PluginInstance(manifest=m_pro, state=PluginState.LOADED)
    assert i_free.is_available("free")
    assert i_free.is_available("pro")
    assert not i_pro.is_available("free")
    assert i_pro.is_available("pro")
