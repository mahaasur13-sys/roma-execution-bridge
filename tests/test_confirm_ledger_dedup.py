"""G-CONFIRM-LEDGER-DOUBLE-WRITE (P3.9 hotfix): идемпотентность аудит-метки.

Дознание №45 (исход А) доказало: подтверждённая задача писала `job.user_confirmed`
дважды — `submit` → `route_job` → `execute_job` → `route_job`, а `write_event`
штамповал новый uuid4 на каждом проходе. Веер писателей был чист: дублировалась
только аудит-метка подтверждения.

Форма лечения предподписана владельцем: идемпотентность записи по ключу
(tenant_id, event_type, entity_id) — второй проход не пишет, если событие уже
стоит; новых таблиц/DDL/миграций нет (G-AUDIT-DDL-DRIFT — отдельная эпоха).
Инвариант хотфикса: `len(events) == 1` на одно подтверждение — включая полный
путь submit → execute.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import audit.event_store as audit_store
import db_adapter as db
import plan_source

PRO = "pro"
TENANT = "dedup-tenant"

_FORCED_VERDICT = {
    "decision": plan_source.REQUIRES_CONFIRMATION,
    "decision_category": "COST_ABOVE_TIER_LIMIT",
    "decision_reason": "принудительный вердикт (hotfix P3.9)",
    "estimated_cost": 41.0,
}


def _patch_tenant(monkeypatch, plan: str = PRO) -> None:
    monkeypatch.setattr(
        db, "get_tenant", lambda tenant_id: {"tenant_id": tenant_id, "plan": plan}
    )
    monkeypatch.setattr(
        db,
        "get_tenant_usage_db",
        lambda tenant_id: {"total_jobs": 0, "total_gpu_seconds": 0},
    )


def _scheduler():
    from scheduler.roma_scheduler import ROMAGPUScheduler

    sched = ROMAGPUScheduler.__new__(ROMAGPUScheduler)
    sched.policy_engine = None
    sched.local_mode = "local"
    sched.gate_unavailable_reason = None
    sched.gpu_connector = SimpleNamespace(is_available=lambda: False)
    sched.cost_gate = SimpleNamespace(
        evaluate=lambda tenant_id, payload: SimpleNamespace(
            result="allowed", reason="gate allowed"
        )
    )
    sched.predictor = SimpleNamespace(predict=lambda **kwargs: dict(_FORCED_VERDICT))
    calls: list = []

    def spy(job):
        calls.append(job.get("job_id"))
        return {
            "status": "success",
            "execution_target": "local",
            "stdout": "SPY_LOCAL_RAN",
        }

    sched._execute_local = spy
    sched._local_calls = calls
    return sched


def _job(job_id: str, **extra):
    job = {
        "job_id": job_id,
        "task_type": "train yolov8",
        "plugin_type": "ml_training",
        "gpu_required": False,
        "command": "python3 -c 'print(1)'",
        "tenant_id": TENANT,
    }
    job.update(extra)
    return job


def _ledger(monkeypatch) -> list:
    """Существующий леджерный путь + ключ идемпотентности (без таблиц/DDL).

    `write_event` — шпион, `event_exists` — состояние шпиона: как в реальном
    append-only леджере, записанный факт становится виден последующим проходам.
    """
    events: list = []
    keys: set = set()

    def spy_event_exists(tenant_id, event_type, entity_id):
        return (tenant_id, event_type, entity_id) in keys

    def spy_write_event(tenant_id, event_type, entity_type, entity_id, data):
        keys.add((tenant_id, event_type, entity_id))
        events.append(
            {
                "tenant_id": tenant_id,
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "data": data,
            }
        )
        return {"id": f"audit-{len(events)}"}

    monkeypatch.setattr(audit_store, "event_exists", spy_event_exists)
    monkeypatch.setattr(audit_store, "write_event", spy_write_event)
    return events


def _executor(sched):
    from scheduler.roma_scheduler import ROMAJobExecutor

    executor = ROMAJobExecutor.__new__(ROMAJobExecutor)
    executor.scheduler = sched
    executor.results = {}
    executor._job_ownership = {}
    return executor


def _confirmed_events(events: list) -> list:
    return [e for e in events if e["event_type"] == "job.user_confirmed"]


# ── инвариант: одно подтверждение — ровно одно событие ──────────────────────


def test_end_to_end_confirmed_job_writes_exactly_one_event(monkeypatch):
    """RED-якорь (№45): полный путь submit → execute давал ДВА события."""
    _patch_tenant(monkeypatch)
    events = _ledger(monkeypatch)
    sched = _scheduler()
    executor = _executor(sched)

    result = asyncio.run(executor.submit(_job("j-dedup-e2e", confirmed=True)))

    assert result["status"] == "success", result
    assert sched._local_calls == ["j-dedup-e2e"], sched._local_calls
    assert len(_confirmed_events(events)) == 1, events


def test_repeated_submit_of_same_job_keeps_single_event(monkeypatch):
    """Повторный submit той же задачи — копии факта нет (ключ идемпотентности)."""
    _patch_tenant(monkeypatch)
    events = _ledger(monkeypatch)

    for _ in range(2):
        sched = _scheduler()
        asyncio.run(_executor(sched).submit(_job("j-dedup-repeat", confirmed=True)))

    assert len(_confirmed_events(events)) == 1, events


def test_two_direct_route_passes_write_once(monkeypatch):
    """Двойной проход route_job на той же задаче (submit → execute_job) — 1 запись."""
    _patch_tenant(monkeypatch)
    events = _ledger(monkeypatch)
    sched = _scheduler()
    job = _job("j-dedup-two-passes", confirmed=True)

    first = sched.route_job(job)
    second = sched.route_job(job)

    assert first["status"] == "queued", first
    assert second["status"] == "queued", second
    assert len(_confirmed_events(events)) == 1, events


def test_different_confirmed_jobs_are_not_glued(monkeypatch):
    """Идемпотентность не склеивает чужие задачи: два job_id → два события."""
    _patch_tenant(monkeypatch)
    events = _ledger(monkeypatch)
    sched = _scheduler()

    sched.route_job(_job("j-dedup-a", confirmed=True))
    sched.route_job(_job("j-dedup-b", confirmed=True))

    confirmed = _confirmed_events(events)
    assert len(confirmed) == 2, events
    assert {e["entity_id"] for e in confirmed} == {"j-dedup-a", "j-dedup-b"}, confirmed


def test_confirmed_jobs_without_job_id_are_not_glued(monkeypatch):
    """T2 (тред #93): нет job_id — нет ключа: два подтверждения → два события.

    Край: задача без job_id фабрикуется напрямую (в живом пути submit её назначает).
    Без ключа идемпотентности нет, но и склейки разных задач под общим entity_id нет.
    """
    _patch_tenant(monkeypatch)
    events = _ledger(monkeypatch)
    sched = _scheduler()

    sched.route_job(_job(None, confirmed=True))
    sched.route_job(_job(None, confirmed=True))

    confirmed = _confirmed_events(events)
    assert len(confirmed) == 2, events
    assert {e["entity_id"] for e in confirmed} == {"unknown"}, confirmed


# ── Trivial (тред #93): негативная кросс-тенантская изоляция через SQL ───────


def test_cross_tenant_isolation_negative(monkeypatch, tmp_path):
    """Trivial (тред #93): изоляция арендаторов проверяется SQL-путём адаптера.

    Шпион `_ledger` сам реализует фильтр по арендатору, поэтому здесь гоняется
    настоящий SQL: `db_adapter.insert_audit_event` / `audit_event_exists` на
    временной sqlite-базе. Схема накатывается продуктовым бутстрапом
    `_ensure_audit_events_table` (G-AUDIT-DDL-DRIFT) — не ручным CREATE.
    """
    import sqlite3

    db_file = tmp_path / "ledger-isolation.db"

    def factory():
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        db._ensure_audit_events_table(conn)
        return conn

    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setattr(db, "_USE_PG", False)
    monkeypatch.setattr(db, "_sqlite_conn", factory)

    tenant_a, tenant_b, job_id = "tenant-a", "tenant-b", "x-shared-job"
    db.insert_audit_event(
        "e-a", tenant_a, "job.user_confirmed", "job", job_id, {"user_confirmed": True}
    )

    assert db.audit_event_exists(tenant_a, "job.user_confirmed", job_id) is True
    assert db.audit_event_exists(tenant_b, "job.user_confirmed", job_id) is False

    db.insert_audit_event(
        "e-b", tenant_b, "job.user_confirmed", "job", job_id, {"user_confirmed": True}
    )

    assert db.audit_event_exists(tenant_b, "job.user_confirmed", job_id) is True
    conn = factory()
    try:
        rows = conn.execute(
            "SELECT tenant_id FROM audit_events WHERE entity_id=? ORDER BY tenant_id",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    assert [r["tenant_id"] for r in rows] == [tenant_a, tenant_b], rows


# ── G-AUDIT-DDL-DRIFT / G-AUDIT-WRITE-ATOMICITY: чистая БД + идемпотентность ──


def _real_sqlite(monkeypatch, tmp_path, db_name="ledger-real.db"):
    """SQLite-зеркало с продуктовым бутстрапом `_ensure_audit_events_table`.

    Таблица не создаётся вручную — накатывается тем же путём, что в продакшене
    для SQLite (`db_adapter._ensure_*`): тест падает, если бутстрап не создаёт
    `audit_events` (G-AUDIT-DDL-DRIFT).
    """
    import sqlite3

    db_file = tmp_path / db_name

    def factory():
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        db._ensure_audit_events_table(conn)
        return conn

    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setattr(db, "_USE_PG", False)
    monkeypatch.setattr(db, "_sqlite_conn", factory)
    return factory, db_file


def _count_audit_rows(factory, tenant_id, event_type, entity_id) -> int:
    conn = factory()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events"
            " WHERE tenant_id=? AND event_type=? AND entity_id=?",
            (tenant_id, event_type, entity_id),
        ).fetchone()
        return row["n"]
    finally:
        conn.close()


def test_write_event_once_idempotent_on_real_sqlite(monkeypatch, tmp_path):
    """Два вызова write_event_once с одним ключом → ровно одна строка (реальный SQL)."""
    factory, _ = _real_sqlite(monkeypatch, tmp_path, "dedup-idem.db")

    first = audit_store.write_event_once(
        TENANT, "job.user_confirmed", "job", "j-idem", {"user_confirmed": True}
    )
    second = audit_store.write_event_once(
        TENANT, "job.user_confirmed", "job", "j-idem", {"user_confirmed": True}
    )

    assert first.get("skipped") is not True, first
    assert second == {"id": None, "skipped": True}, second
    assert _count_audit_rows(factory, TENANT, "job.user_confirmed", "j-idem") == 1


def test_insert_audit_event_dedupes_at_db_level(monkeypatch, tmp_path):
    """G-AUDIT-WRITE-ATOMICITY: два прямых INSERT с одним ключом → одна строка.

    Частичный UNIQUE-индекс + INSERT OR IGNORE закрывают TOCTOU-окно между
    `event_exists` и `write_event_once`: даже минуя прикладную проверку, второй
    INSERT с тем же ключом не создаёт дубль.
    """
    factory, _ = _real_sqlite(monkeypatch, tmp_path, "dedup-atomic.db")

    first = db.insert_audit_event(
        "e-1", TENANT, "job.user_confirmed", "job", "j-atomic", {"user_confirmed": True}
    )
    second = db.insert_audit_event(
        "e-2", TENANT, "job.user_confirmed", "job", "j-atomic", {"user_confirmed": True}
    )

    assert first == {"id": "e-1"}
    assert second == {"id": None, "skipped": True}
    assert _count_audit_rows(factory, TENANT, "job.user_confirmed", "j-atomic") == 1


def test_insert_audit_event_foreign_conflict_raises(monkeypatch, tmp_path):
    """G-AUDIT-WRITE-ATOMICITY (PR #95 :1535): чужой конфликт (PK по id) пробрасывается.

    INSERT OR IGNORE гасил ВСЕ конфликты; узкий ON CONFLICT-таргет под
    audit_events_dedupe_uidx гасит только дедуп-ключ — PK-конфликт = ошибка.
    """
    import sqlite3

    factory, _ = _real_sqlite(monkeypatch, tmp_path, "dedup-foreign.db")

    db.insert_audit_event(
        "dup-id", TENANT, "job.created", "job", "job-A", {"user_confirmed": True}
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_audit_event(
            "dup-id", TENANT, "job.created", "job", "job-B", {"user_confirmed": True}
        )


def test_both_writers_share_table_and_dedupe(monkeypatch, tmp_path):
    """Оба писателя идут в одну таблицу audit_events и подчиняются идемпотентности."""
    import audit_events as root_events

    factory, _ = _real_sqlite(monkeypatch, tmp_path, "dedup-unified.db")

    # корневой писатель (обёртка) → audit.event_store.write_event → audit_events
    root_events.write_audit_event(
        TENANT, "job.created", "job", "j-u1", {"decision_id": "d1"}
    )
    # внутренний идемпотентный писатель с тем же ключом → пропуск, дубля нет
    audit_store.write_event_once(
        TENANT, "job.created", "job", "j-u1", {"decision_id": "d1"}
    )

    assert _count_audit_rows(factory, TENANT, "job.created", "j-u1") == 1


# ── контроли: не-подтверждённые ветки по-прежнему ничего не пишут ───────────


def test_controls_write_no_confirmation_event(monkeypatch):
    """Без флага — отказ; APPROVED — живой путь: событий подтверждения нет."""
    _patch_tenant(monkeypatch)
    events = _ledger(monkeypatch)

    rejected = _scheduler().route_job(_job("j-dedup-noflag"))
    assert rejected["status"] == "rejected", rejected
    assert rejected["reason"] == plan_source.CONFIRMATION_REQUIRED, rejected

    live = _scheduler()
    live.predictor = SimpleNamespace(
        predict=lambda **kwargs: {
            "decision": "APPROVED",
            "decision_category": None,
            "decision_reason": "в пределах лимита",
            "estimated_cost": 1.0,
        }
    )
    approved = live.route_job(_job("j-dedup-approved"))
    assert approved["status"] == "queued", approved
    assert approved["user_confirmed"] is False, approved

    assert events == [], events


# ── структурная наблюдаемость пропуска (без новых таблиц) ───────────────────


def test_write_event_once_reports_skip_structurally(monkeypatch):
    """Пропуск дубля наблюдаем возвратом `skipped: True` и не пишет в леджер."""
    written: list = []
    monkeypatch.setattr(audit_store, "event_exists", lambda t, e, i: True)
    monkeypatch.setattr(
        audit_store,
        "write_event",
        lambda *a, **k: written.append(a) or {"id": "must-not-happen"},
    )

    result = audit_store.write_event_once(
        TENANT, "job.user_confirmed", "job", "j-dedup-skip", {"user_confirmed": True}
    )

    assert result == {"id": None, "skipped": True}, result
    assert written == [], written
