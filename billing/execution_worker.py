"""Background execution worker + billing loop for ROMA Execution Bridge.

Цикл после submit: dispatch → wait → bill → cleanup.
Работает асинхронно после возврата 202 от submit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("roma.execution_worker")

# Lazy refs — устанавливаются при init_worker()
_billing_ledger = None
_db_adapter = None
_backend_manager = None


def init_worker():
    """Вызывается один раз при старте приложения. Устанавливает ссылки на компоненты."""
    global _billing_ledger, _db_adapter, _backend_manager
    from billing.pg_ledger import PGBillingLedger as BL
    import db_adapter as db
    from backends.dispatcher import dispatch_job, backend_cancel_job, get_job_status

    _billing_ledger = BL()
    _db_adapter = db
    _backend_manager = {"dispatch": dispatch_job, "cancel": backend_cancel_job, "status": get_job_status}
    logger.info("execution_worker initialized")


async def poll_and_execute():
    """Фоновый цикл: опрашивает БД на наличие queued-джобов и запускает execute_and_bill."""
    logger.info("poll_and_execute started")
    while True:
        try:
            jobs = _db_adapter.get_queued_execution_jobs(limit=5)
            for job in jobs:
                payload = job.get("payload", {}) or {}
                # Local-задачи выполняет внешний local_worker через safe_exec.
                # Не трогаем их здесь, иначе local_worker не найдёт их в queued.
                backend = (job.get("backend") or payload.get("backend") or "").lower()
                if backend == "local":
                    continue
                jid = job["id"]
                tid = job["tenant_id"]
                # Переводим из queued в running и запускаем
                _db_adapter.update_execution_job(jid, status="running")
                asyncio.ensure_future(execute_and_bill(jid, tid, payload))
                logger.info("poll_and_execute.started job=%s tenant=%s", jid, tid)
        except Exception as e:
            logger.warning("poll_and_execute.error: %s", e)
        await asyncio.sleep(5)


async def execute_and_bill(
    job_id: str,
    tenant_id: str,
    payload: dict,
) -> dict:
    """Полный цикл выполнения: dispatch → poll → bill → cleanup.

    Шаги:
    1. Dispatch job на активный backend (vastai/local)
    2. Ждать завершения (poll статус)
    3. Рассчитать стоимость: duration_seconds * цена GPU
    4. Списать с баланса тенанта (debit)
    5. Записать usage_event
    6. Уничтожить инстанс (для vastai)
    """
    logger.info("execute_and_bill.start job=%s tenant=%s", job_id, tenant_id)
    start_time = time.monotonic()

    # Шаг 1: Dispatch
    try:
        result = await _backend_manager["dispatch"](
            job_id=job_id, tenant_id=tenant_id, payload=payload
        )
        backend_name = result.get("backend", "local")
        price_per_hour = result.get("price_per_hour", 0.0)
        contract_id = result.get("contract_id")

        if result.get("status") == "failed":
            logger.warning("execute_and_bill.dispatch_failed job=%s: %s", job_id, result.get("message", ""))
            _db_adapter.update_execution_job(
                job_id,
                status="failed",
                backend=backend_name if backend_name != "local" else payload.get("backend"),
                error=str(result.get("message", "dispatch failed"))[:500],
            )
            return {"status": "failed", "job_id": job_id, "error": result.get("message", "")}

        _db_adapter.update_execution_job(job_id, status="running", backend=backend_name,
                                          backend_job_id=str(contract_id) if contract_id else None)
    except Exception as exc:
        logger.error("execute_and_bill.dispatch_failed job=%s: %s", job_id, exc)
        _db_adapter.update_execution_job(job_id, status="failed", error=str(exc)[:500])
        return {"status": "failed", "error": str(exc)}

    # Шаг 2: Poll до завершения (макс 10 мин для GPU, 2 мин для local)
    max_wait = 600 if backend_name == "vastai" else 120
    poll_interval = 15 if backend_name == "vastai" else 5
    waited = 0.0
    status = "unknown"

    while waited < max_wait:
        try:
            status_info = await _backend_manager["status"](job_id, str(contract_id) if contract_id else None)
            status = status_info.get("status", "unknown")

            if status in ("completed", "failed", "error", "stopped", "cancelled", "destroyed"):
                break

            # Если инстанс запущен — пытаемся выполнить команду
            if status == "running":
                ssh_host = status_info.get("ssh_host", status_info.get("host", ""))
                if ssh_host:
                    cmd_result = await _execute_command(job_id, payload, tenant_id)
                    status = "completed" if cmd_result.get("status") == "completed" else "failed"
                    break

        except Exception as exc:
            logger.warning("execute_and_bill.poll_error job=%s: %s", job_id, exc)

        await asyncio.sleep(poll_interval)
        waited += poll_interval

    # Таймаут
    if waited >= max_wait:
        status = "timeout"
        logger.warning("execute_and_bill.timeout job=%s waited=%.0fs", job_id, waited)

    # Шаг 3: Расчёт стоимости
    elapsed = time.monotonic() - start_time
    elapsed_hours = elapsed / 3600.0
    cost_usd = round(elapsed_hours * price_per_hour, 6) if price_per_hour > 0 else round(elapsed_hours * 0.002, 6)

    # Шаг 4: Списание с баланса
    now_iso = datetime.now(timezone.utc).isoformat()
    billing_ok = False
    try:
        balance_before = _billing_ledger.get_balance(tenant_id) or 0.0
        if balance_before >= cost_usd:
            _billing_ledger.debit(
                tenant_id, cost_usd, "USD",
                job_id=job_id, backend=backend_name, status=status,
            )
            billing_ok = True
            logger.info("execute_and_bill.debited tenant=%s amount=%.6f job=%s", tenant_id, cost_usd, job_id)
        else:
            logger.warning("execute_and_bill.insufficient_funds tenant=%s balance=%.4f cost=%.4f",
                          tenant_id, balance_before, cost_usd)

        # Шаг 5: Запись usage_event
        _db_adapter.record_usage_event(
            tenant_id, "gpu_execution", elapsed, cost_usd, job_id,
            {"backend": backend_name, "price_per_hour": price_per_hour}
        )
        logger.info("execute_and_bill.usage_recorded tenant=%s job=%s", tenant_id, job_id)

    except Exception as exc:
        logger.error("execute_and_bill.billing_error job=%s: %s", job_id, exc)

    # Обновляем статус job в БД
    _db_adapter.update_execution_job(job_id, status=status,
                                      completed_at=now_iso, cost_usd=cost_usd,
                                      duration_seconds=elapsed,
                                      error="" if billing_ok else "Billing error")

    # Шаг 6: Уничтожить инстанс (только для vastai)
    if backend_name == "vastai" and contract_id:
        try:
            await _backend_manager["cancel"](job_id, tenant_id)
        except Exception as exc:
            logger.warning("execute_and_bill.cleanup_failed job=%s: %s", job_id, exc)

    return {
        "status": status,
        "job_id": job_id,
        "tenant_id": tenant_id,
        "cost_usd": cost_usd,
        "duration_seconds": round(elapsed, 2),
        "backend": backend_name,
    }


async def _execute_command(job_id: str, payload: dict, tenant_id: str) -> dict:
    """Запустить команду на Vast.ai инстансе."""
    cmd = payload.get("task", payload.get("command", "echo OK"))
    try:
        from backends.dispatcher import get_backend
        backend = get_backend(payload.get("backend"))
        from backends.base import JobContext
        ctx = JobContext(job_id=job_id, tenant_id=tenant_id, payload=payload)
        result = await backend.run_command(ctx, cmd, timeout=300)
        return result
    except Exception as e:
        logger.warning("execute_command_failed job=%s: %s", job_id, e)
        return {"status": "failed", "output": str(e)}


def bill_job(job_id: str, tenant_id: str, payload: dict) -> asyncio.Task:
    """Запускает execute_and_bill как фоновую задачу. Возвращает asyncio.Task."""
    loop = asyncio.get_event_loop()
    return loop.create_task(execute_and_bill(job_id, tenant_id, payload))
