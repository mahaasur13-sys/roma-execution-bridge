"""G-QUOTA-SOURCE-REMNANTS-2 / Q-3: мёртвые модули tier_gate.py/white_label.py снесены.

Гейт «0 живых REFS» (пройден): 0 импортов, 0 символьных использований, 0 динамики,
0 saas/, 0 entry-points. Модули снесены целиком; битый `db.get_tier_profile` не чинился.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dead_tier_modules_are_removed():
    assert importlib.util.find_spec("tier_gate") is None
    assert importlib.util.find_spec("white_label") is None
    assert not (REPO_ROOT / "tier_gate.py").exists()
    assert not (REPO_ROOT / "white_label.py").exists()
