"""N-BLACK-B: машина состояний control-plane — переходы, ретраи, персистентность.

Зачем: control_plane/job_store.py — единственное место, где живёт допустимый
порядок переходов job (SUBMITTED → SCHEDULED → RUNNING → COMPLETED → COMMITTED
и ветка FAILED → PENDING_RETRY → DEAD). Модуль не исполнялся ни одним тестом,
то есть и сам запрет на нелегальные переходы, и выживание состояния после
перезапуска процесса были не проверены. Здесь проверяются именно переходы,
а не строки: каждая проверка формулирует ожидаемое поведение.

Граничные случаи (нелегальный переход отклоняется, потеря персистентности,
исчерпание ретраев) ранее не наблюдались — это и есть цель набора.
"""

import time

import pytest

from control_plane.core_models import GPULease, Job, JobStatus, Worker, WorkerStatus
from control_plane.job_store import JobStore


class TestCoreModels:
    def test_worker_health_and_free_gpu(self):
        worker = Worker(id="w1", status=WorkerStatus.HEALTHY, gpu_total=4.0, gpu_used=1.5)
        assert worker.gpu_free() == pytest.approx(2.5)
        assert worker.is_healthy() is True

        worker.gpu_used = 9.0
        assert worker.gpu_free() == 0.0, "свободная ёмкость не может стать отрицательной"

        worker.status = WorkerStatus.DEAD
        assert worker.is_healthy() is False

    def test_worker_touch_advances_heartbeat(self):
        worker = Worker(id="w1")
        before = worker.last_heartbeat
        time.sleep(0.01)
        worker.touch()
        assert worker.last_heartbeat > before

    def test_gpu_lease_expiry_scales_with_renewals(self):
        fresh = GPULease(gpu_id="g0", job_id="j1", worker_id="w1", ttl=30.0)
        assert fresh.is_expired() is False

        old = GPULease(gpu_id="g0", job_id="j1", worker_id="w1", ttl=30.0, created_at=time.time() - 31)
        assert old.is_expired() is True

        renewed = GPULease(
            gpu_id="g0", job_id="j1", worker_id="w1", ttl=10.0, created_at=time.time() - 15, renewed=2
        )
        assert renewed.is_expired() is False, "продление аренды обязано отодвигать срок истечения"

    def test_job_terminal_and_retryability(self):
        job = Job(id="j1", plugin="ml_training", payload={})
        assert job.is_terminal() is False

        for status in (JobStatus.COMPLETED, JobStatus.COMMITTED, JobStatus.DEAD, JobStatus.FAILED):
            job.status = status
            assert job.is_terminal() is True
            job.status = JobStatus.SUBMITTED

        job.status = JobStatus.FAILED
        assert job.is_retryable() is True
        job.retry_count = job.max_retries
        assert job.is_retryable() is False, "исчерпанный лимит ретраев закрывает повтор"

        job.retry_count = 0
        job.status = JobStatus.RUNNING
        assert job.is_retryable() is False


class TestJobStoreStateMachine:
    def test_fresh_store_is_empty_and_sequenced(self, tmp_path):
        store = JobStore(str(tmp_path / "jobs.json"))
        assert store.stats()[JobStatus.SUBMITTED.value] == 0

        job = store.submit("ml_training", {"batch_size": 4}, gpu=2.0, ttl=120.0, max_retries=1)
        assert job.id.startswith("job-00001-")
        assert job.status is JobStatus.SUBMITTED
        assert job.gpu_allocated == pytest.approx(2.0)
        assert job.ttl_seconds == pytest.approx(120.0)
        assert job.max_retries == 1
        assert store.get(job.id) is job

    def test_happy_path_transitions_and_illegal_ones_are_refused(self, tmp_path):
        store = JobStore(str(tmp_path / "jobs.json"))
        job = store.submit("inference", {})

        assert store.start(job.id) is False, "нельзя стартовать до назначения на воркер"
        assert store.complete(job.id) is False, "нельзя завершить до старта"
        assert store.commit(job.id) is False, "нельзя коммитить до завершения"

        assert store.schedule(job.id, "w-1") is True
        assert job.worker_id == "w-1"
        assert job.status is JobStatus.SCHEDULED
        assert store.schedule(job.id, "w-2") is False, "повторное назначение запрещено"

        assert store.start(job.id) is True
        assert store.complete(job.id) is True
        assert store.commit(job.id) is True
        assert job.status is JobStatus.COMMITTED
        assert job.is_terminal() is True

    def test_unknown_job_is_never_mutated(self, tmp_path):
        store = JobStore(str(tmp_path / "jobs.json"))
        assert store.schedule("job-missing", "w-1") is False
        assert store.start("job-missing") is False
        assert store.complete("job-missing") is False
        assert store.commit("job-missing") is False
        assert store.fail("job-missing", "boom") is False

    def test_failure_records_error_and_requires_active_state(self, tmp_path):
        store = JobStore(str(tmp_path / "jobs.json"))
        job = store.submit("ml_training", {})

        assert store.fail(job.id, "before scheduling") is False, "SUBMITTED — не активное состояние"
        store.schedule(job.id, "w-1")
        assert store.fail(job.id, "cuda oom") is True
        assert job.status is JobStatus.FAILED
        assert job.error == "cuda oom"
        assert store.list_failed() == [job]

    def test_requeue_counts_attempts_then_declares_dead(self, tmp_path):
        store = JobStore(str(tmp_path / "jobs.json"))
        job = store.submit("ml_training", {}, max_retries=1)

        store.schedule(job.id, "w-1")
        store.fail(job.id, "transient")
        assert store.requeue(job.id) is True
        assert job.status is JobStatus.PENDING_RETRY
        assert job.retry_count == 1
        assert job.worker_id is None, "повторная попытка идёт заново к свободному воркеру"

        assert store.advance_pending() == [job]
        assert job.status is JobStatus.SUBMITTED

        store.schedule(job.id, "w-2")
        store.fail(job.id, "transient again")
        assert store.requeue(job.id) is False, "лимит ретраев исчерпан"
        assert job.status is JobStatus.DEAD
        assert job.is_terminal() is True

        assert store.advance_pending() == [], "DEAD не возвращается в очередь"

    def test_advance_pending_returns_only_retry_queue(self, tmp_path):
        store = JobStore(str(tmp_path / "jobs.json"))
        waiting = store.submit("ml_training", {})
        untouched = store.submit("inference", {})

        store.schedule(waiting.id, "w-1")
        store.fail(waiting.id, "boom")
        store.requeue(waiting.id)

        assert store.advance_pending() == [waiting]
        assert untouched.status is JobStatus.SUBMITTED

    def test_state_survives_process_restart(self, tmp_path):
        path = str(tmp_path / "jobs.json")
        store = JobStore(path)
        job = store.submit("ml_training", {"epochs": 2})
        store.schedule(job.id, "w-7")

        restarted = JobStore(path)
        restored = restarted.get(job.id)
        assert restored is not None, "состояние джобы обязано переживать перезапуск"
        # Находка N-BLACK-B-1: при чтении с диска status приходит строкой ('SCHEDULED'),
        # а не членом JobStatus — тождество типа при персистентности не сохраняется.
        # Логика состояний сравнивает значения (str-Enum), поэтому переходы продолжают
        # работать; расхождение зафиксировано, решение — за владельцем.
        assert restored.status == JobStatus.SCHEDULED
        assert isinstance(restored.status, str) and not isinstance(restored.status, JobStatus)
        assert restarted.stats()[JobStatus.SCHEDULED.value] == 1
        assert restarted.list_pending() == []
        assert restored.worker_id == "w-7"
        assert restored.payload == {"epochs": 2}

        next_job = restarted.submit("inference", {})
        assert next_job.id.startswith("job-00002-"), "нумерация продолжается после перезапуска"

    def test_listing_and_stats_reflect_state(self, tmp_path):
        store = JobStore(str(tmp_path / "jobs.json"))
        pending = store.submit("ml_training", {})
        running = store.submit("inference", {})
        store.schedule(running.id, "w-1")
        store.start(running.id)

        assert store.list_pending() == [pending]
        assert store.list_by_worker("w-1") == [running]
        assert store.list_by_worker("w-absent") == []

        stats = store.stats()
        assert stats[JobStatus.SUBMITTED.value] == 1
        assert stats[JobStatus.RUNNING.value] == 1
        assert stats[JobStatus.COMMITTED.value] == 0
        assert sum(stats.values()) == 2
