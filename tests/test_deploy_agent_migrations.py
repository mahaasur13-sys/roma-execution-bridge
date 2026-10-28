"""Deploy-агент гоняет миграции через apply_migrations.py под админским DSN.

PG-free: `do_deploy` прогоняется на моках git/супервизора, поэтому проверяется ровно
контракт выката — какой раннер вызван, что происходит без файла DSN и при rc≠0,
и что коннект не утекает в журнал/алерт.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_PATH = REPO_ROOT / "deploy" / "ops" / "zo_deploy_agent.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SHA = "a" * 40
DSN_LEAK = "postgresql://admin:s3cr3t-value@127.0.0.1:5432/roma"


def _load_agent():
    spec = importlib.util.spec_from_file_location("zo_deploy_agent_under_test", str(AGENT_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Harness:
    def __init__(self) -> None:
        self.migrate_rc = 0
        self.migrate_err = ""
        self.calls: list[list[str]] = []
        self.failed: list[tuple[str, str]] = []
        self.rolled_back = 0
        self.restarted = 0
        self.emitted: list[tuple[str, dict]] = []
        self.pending = [["019_x.sql"], []]

    def run(self, cmd, *, timeout, cwd=None):  # noqa: ANN001, ARG002
        self.calls.append(list(cmd))
        if any("apply_migrations.py" in part for part in cmd):
            return self.migrate_rc, "", self.migrate_err
        return 0, "", ""

    def pending_migrations(self):
        return self.pending.pop(0), True


@pytest.fixture()
def agent(monkeypatch, tmp_path):
    module = _load_agent()
    h = Harness()

    monkeypatch.setattr(module, "ADMIN_DSN_FILE", str(tmp_path / "roma-pg-dsn.txt"))
    monkeypatch.setattr(module, "run", h.run)
    monkeypatch.setattr(module, "ff_possible", lambda sha: (True, ""))
    monkeypatch.setattr(module, "pending_migrations", h.pending_migrations)
    monkeypatch.setattr(module, "save_state", lambda state: None)
    monkeypatch.setattr(module, "post_status", lambda *a, **k: None)
    monkeypatch.setattr(module, "alert", lambda *a, **k: None)
    monkeypatch.setattr(module, "emit", lambda event, **fields: h.emitted.append((event, fields)))
    monkeypatch.setattr(module, "fail", lambda state, sha, reason, detail, **k: h.failed.append((reason, detail)))
    monkeypatch.setattr(module, "rollback_tree", lambda previous, state: setattr(h, "rolled_back", h.rolled_back + 1))

    def _restart():
        h.restarted += 1
        return True, "ok"

    monkeypatch.setattr(module, "restart_service", _restart)
    monkeypatch.setattr(module, "wait_healthy", lambda: (True, "ok"))
    module._harness = h
    module._dsn_path = tmp_path / "roma-pg-dsn.txt"
    return module


def _deploy(module) -> bool:
    return module.do_deploy(SHA, {"deployed_sha": ""}, dry_run=False, rollback_on_unhealthy=False)


def test_migration_runs_apply_migrations_with_admin_dsn_file(agent):
    agent._dsn_path.write_text("postgresql://admin:pw@127.0.0.1:5432/roma\n", encoding="utf-8")

    assert _deploy(agent) is True

    migrate = [c for c in agent._harness.calls if any("apply_migrations.py" in part for part in c)]
    assert migrate == [[agent.PYTHON, "scripts/apply_migrations.py", "--dsn-file", str(agent._dsn_path)]]
    assert not [c for c in agent._harness.calls if any("run_migrations.py" in part for part in c)]
    assert agent._harness.restarted == 1
    assert agent._harness.failed == []


def test_missing_dsn_file_fails_before_migrating_and_never_restarts(agent):
    assert _deploy(agent) is False

    assert not [c for c in agent._harness.calls if any("apply_migrations.py" in part for part in c)]
    assert agent._harness.failed and agent._harness.failed[0][0] == "migration_failed"
    assert agent._harness.rolled_back == 1
    assert agent._harness.restarted == 0


@pytest.mark.parametrize("rc", [1, 2])
def test_runner_failure_rolls_the_tree_back(agent, rc):
    agent._dsn_path.write_text("postgresql://admin:pw@127.0.0.1:5432/roma\n", encoding="utf-8")
    agent._harness.migrate_rc = rc
    agent._harness.migrate_err = "role roma_owner is missing — refusing (fail-closed)"

    assert _deploy(agent) is False

    assert [r for r, _ in agent._harness.failed] == ["migration_failed"]
    assert agent._harness.rolled_back == 1
    assert agent._harness.restarted == 0


def test_dsn_never_reaches_alert_or_journal(agent):
    agent._dsn_path.write_text("postgresql://admin:pw@127.0.0.1:5432/roma\n", encoding="utf-8")
    agent._harness.migrate_rc = 1
    agent._harness.migrate_err = f"OperationalError: could not connect to {DSN_LEAK}\nTraceback: ..."

    assert _deploy(agent) is False

    _, detail = agent._harness.failed[0]
    assert "s3cr3t-value" not in detail
    assert "postgresql://" not in detail
    assert len(detail) <= 200

    logged = [fields for event, fields in agent._harness.emitted if event == "migrations_run"]
    assert logged, "migrations_run must still be emitted"
    assert "s3cr3t-value" not in str(logged[0])
    assert "postgresql://" not in str(logged[0])
