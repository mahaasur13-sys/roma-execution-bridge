"""Deploy-агент не выкатывает SHA, у которого изменились зависимости venv (фаза 5.1, PR 1).

Агент не ставит зависимости: если между выкаченным и целевым SHA изменился
`requirements-runtime.txt` или `uv.lock`, выкат обязан остановиться ДО `git merge --ff-only` —
без compileall, миграций, рестарта и `reset --hard`, с отказом `deps_changed` в памяти по SHA.
Не удалось сравнить деревья — тот же fail-closed с причиной `deps_diff_unreadable`;
неизвестен выкаченный SHA — `deps_base_unknown`, без git diff (HEAD базой не считается).
PG-free: git/супервизор на моках `run()`, `deps_changed()` дополнительно — на настоящем git в tmp.
"""

from __future__ import annotations

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_PATH = REPO_ROOT / "deploy" / "ops" / "zo_deploy_agent.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PREV = "b" * 40
SHA = "a" * 40
OTHER = "c" * 40  # постороннее отказавшее SHA — его запись обязана остаться нетронутой
OLD_TS = "2026-10-20T08:00:00Z"
DIFF = ["git", "diff", "--name-only"]
FF = ["git", "merge", "--ff-only"]


def _load_agent():
    spec = importlib.util.spec_from_file_location("zo_deploy_agent_deps_under_test", str(AGENT_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Harness:
    """Пишет каждую внешнюю команду агента по порядку; супервизор и git — только через run()."""

    def __init__(self) -> None:
        self.diff_rc = 0
        self.diff_out = ""
        self.calls: list[list[str]] = []
        self.alerts: list[tuple] = []
        self.statuses: list[tuple] = []

    def run(self, cmd, *, timeout, cwd=None):  # noqa: ANN001, ARG002
        self.calls.append(list(cmd))
        if cmd[:3] == DIFF:
            return self.diff_rc, self.diff_out, "fatal: bad object" if self.diff_rc else ""
        return 0, "", ""

    def index(self, prefix: list[str]) -> int:
        return next(i for i, c in enumerate(self.calls) if c[: len(prefix)] == prefix)

    def has(self, needle: str) -> bool:
        return any(needle in part for c in self.calls for part in c)


@pytest.fixture()
def agent(monkeypatch, tmp_path):
    module = _load_agent()
    h = Harness()
    monkeypatch.setattr(module, "STATE_DIR", tmp_path)
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "LOG_PATH", tmp_path / "deploy-agent.log")
    monkeypatch.setattr(module, "ADMIN_DSN_FILE", str(tmp_path / "roma-pg-dsn.txt"))
    monkeypatch.setattr(module, "run", h.run)
    monkeypatch.setattr(module, "ff_possible", lambda sha: (True, ""))
    monkeypatch.setattr(module, "pending_migrations", lambda: ([], True))
    monkeypatch.setattr(module, "ahead_by", lambda base, head: None)
    monkeypatch.setattr(module, "post_status", lambda *a, **k: h.statuses.append(a))
    monkeypatch.setattr(module, "alert", lambda *a, **k: h.alerts.append(a))
    monkeypatch.setattr(module, "wait_healthy", lambda: (True, "ok"))
    # Время заморожено на «сейчас»: fail() и save_state() пишут now_iso() в state, и тест
    # сверяет точные значения, а не «что-то поменялось». Реальное «сейчас», а не дата из
    # прошлого: cycle() считает возраст last_failed_notice по настоящим часам.
    frozen = module.now_iso()
    monkeypatch.setattr(module, "now_iso", lambda: frozen)
    module._now = frozen
    module._harness = h
    return module


def _state(deployed_sha: str = PREV) -> dict:
    """Реалистичный state.json живого агента: bootstrap-метка, история, старые отказы."""
    return {
        "deployed_sha": deployed_sha,
        "deployed_sha_source": "inferred_from_head",
        "history": [{"sha": PREV, "at": "2026-10-30T10:00:00Z", "steps": ["fetch", "ff", "restart"], "commits": 1}],
        "last_success": "2026-10-30T10:00:00Z",
        "last_failed_notice": OLD_TS,
        "notices": {"failed_memory:" + OTHER: OLD_TS},
        "failed": {OTHER: {"reason": "ci_red", "detail": "CI по SHA завершился: failure", "ts": OLD_TS}},
        "updated_at": "2026-10-30T10:00:00Z",
    }


def _deploy(module, state: dict) -> bool:
    return module.do_deploy(SHA, state, dry_run=False, rollback_on_unhealthy=False)


def _expected_after_refusal(module, initial: dict) -> dict:
    """Какие ключи вне `failed` законно меняет отказ в do_deploy — и на что именно.

    * last_failed_notice: fail() безусловно ставит now_iso() — это отметка первого алерта,
      от неё cycle() отсчитывает час до «стоячего» напоминания. Ожидается ровно now.
    * updated_at: save_state() ставит now_iso() при каждой записи. Ожидается ровно now.
    Всё остальное (deployed_sha, deployed_sha_source, history, last_success, notices) — как было.
    """
    expected = {k: v for k, v in copy.deepcopy(initial).items() if k != "failed"}
    expected["last_failed_notice"] = module._now
    expected["updated_at"] = module._now
    return expected


def _assert_refused_untouched(module, initial: dict, state: dict, reason: str, last_call: list[str]) -> None:
    h = module._harness
    expected = _expected_after_refusal(module, initial)
    persisted = module.load_state()
    for label, got in (("в памяти", state), ("после перечтения state.json", persisted)):
        assert {k: v for k, v in got.items() if k != "failed"} == expected, label
        assert set(got["failed"]) == {OTHER, SHA}, label
        assert got["failed"][OTHER] == initial["failed"][OTHER], f"чужой отказ изменён ({label})"
        assert got["failed"][SHA]["reason"] == reason, label
        assert got["failed"][SHA]["ts"] == module._now, label
        assert got["failed"][SHA]["detail"], label
    # ни fast-forward, ни предпроверки, ни миграций, ни рестарта, ни отката дерева
    assert not [c for c in h.calls if c[:3] == FF]
    assert not h.has("compileall") and not h.has("apply_migrations.py")
    assert not h.has("supervisorctl") and not h.has("restart")
    assert not [c for c in h.calls if c[:2] == ["git", "reset"]]
    # последняя внешняя команда — та, на которой отказ: после него агент ничего не запускал
    assert h.calls[-1][: len(last_call)] == last_call
    assert len(h.alerts) == 1 and reason in h.alerts[0][1]
    assert h.statuses[-1][0] == SHA and h.statuses[-1][1] == "error"


@pytest.mark.parametrize("changed", ["requirements-runtime.txt", "uv.lock", "requirements-runtime.txt\nuv.lock"])
def test_deps_change_blocks_before_fast_forward(agent, changed):
    h = agent._harness
    h.diff_out = changed
    state = _state()
    initial = copy.deepcopy(state)

    assert _deploy(agent, state) is False

    _assert_refused_untouched(agent, initial, state, "deps_changed", DIFF)
    for name in changed.split("\n"):
        assert name in state["failed"][SHA]["detail"]
    # порядок: fetch → diff (между выкаченным и целевым SHA, только по файлам зависимостей)
    assert h.index(["git", "fetch"]) < h.index(DIFF)
    assert [c for c in h.calls if c[:3] == DIFF] == [[*DIFF, PREV, SHA, "--", *agent.DEPS_FILES]]


def test_unreadable_diff_fails_closed(agent):
    agent._harness.diff_rc = 128  # например, deployed_sha исчез из локальной истории после force-push
    state = _state()
    initial = copy.deepcopy(state)

    assert _deploy(agent, state) is False

    _assert_refused_untouched(agent, initial, state, "deps_diff_unreadable", DIFF)


def test_unrelated_changes_take_the_normal_path(agent):
    h = agent._harness
    h.diff_out = ""  # git diff -- <deps files> пуст: менялся только код
    state = _state()

    assert _deploy(agent, state) is True

    assert state["deployed_sha"] == SHA
    assert SHA not in state["failed"]
    assert h.index(["git", "fetch"]) < h.index(DIFF) < h.index(FF), "проверка deps — до fast-forward"
    assert h.has("compileall") and h.has("supervisorctl")
    assert "deps:unchanged" in state["history"][-1]["steps"]


def test_empty_deployed_sha_refuses_without_diff(agent):
    """Пустой deployed_sha: HEAD не принимается за состояние прода — отказ до git diff."""
    h = agent._harness
    state = _state(deployed_sha="")
    initial = copy.deepcopy(state)

    assert _deploy(agent, state) is False

    _assert_refused_untouched(agent, initial, state, "deps_base_unknown", ["git", "fetch"])
    assert not [c for c in h.calls if c[:3] == DIFF], "без базы git diff не вызывается"
    assert [c[:2] for c in h.calls] == [["git", "fetch"]], "fetch — и больше ничего"


@pytest.mark.parametrize(
    ("deployed", "diff_rc", "diff_out", "reason"),
    [
        (PREV, 0, "uv.lock", "deps_changed"),
        (PREV, 128, "", "deps_diff_unreadable"),
        ("", 0, "", "deps_base_unknown"),
    ],
)
def test_refused_sha_is_not_retried(agent, monkeypatch, deployed, diff_rc, diff_out, reason):
    h = agent._harness
    h.diff_rc, h.diff_out = diff_rc, diff_out
    monkeypatch.setattr(agent, "remote_sha", lambda: SHA)
    ci_calls = []
    monkeypatch.setattr(agent, "ci_state", lambda sha: ci_calls.append(sha) or ("green", "ci success"))
    state = _state(deployed_sha=deployed)

    state = agent.cycle(state, dry_run=False, rollback_on_unhealthy=False, heartbeat_due=False)
    assert state["failed"][SHA]["reason"] == reason
    assert ci_calls == [SHA]
    calls_after_refusal, alerts_after_refusal = len(h.calls), len(h.alerts)
    state_after_refusal = copy.deepcopy(state)
    file_after_refusal = agent.STATE_PATH.read_bytes()

    for _ in range(3):
        state = agent.cycle(state, dry_run=False, rollback_on_unhealthy=False, heartbeat_due=True)

    assert ci_calls == [SHA], "CI-гейт для SHA из памяти не спрашивается"
    assert len(h.calls) == calls_after_refusal, "ни fetch, ни diff, ни рестарта в повторных циклах"
    assert len(h.alerts) == alerts_after_refusal, "стоячий алерт — не раньше часа после отказа"
    # повторные циклы по SHA из памяти не трогают state вовсе: ни в памяти, ни на диске
    # (last_failed_notice свежий → стоячий алерт не положен → notice_due/save_state не зовутся)
    assert state == state_after_refusal
    assert agent.STATE_PATH.read_bytes() == file_after_refusal
    assert state["deployed_sha"] == deployed


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_deps_changed_on_real_git(tmp_path, monkeypatch):
    module = _load_agent()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "uv.lock").write_text("a==1\n")
    (repo / "requirements-runtime.txt").write_text("a==1\n")
    (repo / "pyproject.toml").write_text("[tool.ruff]\n")
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "app.py").write_text("x = 2\n")
    (repo / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n")
    _git(repo, "commit", "-qam", "code only")
    code_only = _git(repo, "rev-parse", "HEAD")

    (repo / "uv.lock").write_text("a==2\n")
    _git(repo, "commit", "-qam", "deps")
    deps = _git(repo, "rev-parse", "HEAD")

    real_run = module.run
    monkeypatch.setattr(module, "run", lambda cmd, *, timeout, cwd=repo: real_run(cmd, timeout=timeout, cwd=repo))

    assert module.deps_changed(base, code_only) == (False, "")
    assert module.deps_changed(base, deps) == (True, "uv.lock")
    assert module.deps_changed(code_only, deps) == (True, "uv.lock")
    changed, detail = module.deps_changed("f" * 40, deps)
    assert changed is None and "rc=" in detail
