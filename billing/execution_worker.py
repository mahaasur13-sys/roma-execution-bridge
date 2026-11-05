"""Backg_price_per_hourn worker + billing loop for ROMA Execution Bridge.

Цикл после submit: dispatch → wait → bill → cleanup.
Работает асинхронно после возврата 202 от submit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from billing.pg_ledger import PGUnavailableError

logger = logging.getLogger("roma.execution_worker")

# Lazy refs — устанавливаются при init_worker()
_billing_ledger = None
_db_adapter = None
_backend_manager = None


_INFLIGHT = set()

# Потолок одного задания: _execute_and_bill_impl ждёт до 600s для GPU (vastai)
# и до 120s для local. Осиротевшим считаем running, провисевший вдвое дольше
# самого длинного цикла: воркер, который его вёл, уже не вернётся.
MAX_JOB_TIMEOUT_SECONDS = 600
JOB_ORPHAN_TIMEOUT_SECONDS = int(os.environ.get("JOB_ORPHAN_TIMEOUT", str(MAX_JOB_TIMEOUT_SECONDS * 2)))
_ORPHAN_SWEEP_INTERVAL_S = 300.0
_last_orphan_sweep = 0.0

# Свипер billing_pending: задание выполнено, но дебет не сохранился
# (pg_error / error / no_funds). Повторный finalize идемпотентен по ключу
# job:<id> (pg_ledger.debit_if_funds), так что двойного DEBIT быть не может.
BILLING_PENDING_BATCH = int(os.environ.get("BILLING_PENDING_BATCH", "50"))
_BILLING_PENDING_SWEEP_INTERVAL_S = 60.0
# no_funds не лечится сам — не долбим ledger и лог каждую минуту.
BILLING_PENDING_NO_FUNDS_RETRY_S = float(os.environ.get("BILLING_PENDING_NO_FUNDS_RETRY_S", "3600"))
_last_billing_pending_sweep = 0.0
_no_funds_next_try: dict[str, float] = {}
# C10: refund_pending-свипер (временный костыль, читает error до DDL).
_REFUND_PENDING_SWEEP_INTERVAL_S = 60.0
_last_refund_pending_sweep = 0.0
_TERMINAL_STATUSES = ("completed", "timeout", "failed", "cancelled")
# G5: финальная запись воркера не перезаписывает статус, выставленный другим
# путём, пока задача исполнялась (/cancel, /complete, свипер, drain). Набор =
# db_adapter.CANCEL_BLOCKED_STATUSES: терминальные + сырые backend-статусы +
# billing_pending.
_FINAL_WRITE_BLOCKED_STATUSES = _TERMINAL_STATUSES + ("error", "stopped", "destroyed", "billing_pending")


def _track(task):
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)
    return task


async def drain_inflight(grace_s: float = 15.0):
    pending = [t for t in list(_INFLIGHT) if not t.done()]
    if not pending:
        return
    _done, still = await asyncio.wait(pending, timeout=grace_s)
    for t in still:
        t.cancel()
    if still:
        await asyncio.gather(*still, return_exceptions=True)


def init_worker():
    """Вызывается один раз при старте приложения. Устанавливает ссылки на компоненты."""
    global _billing_ledger, _db_adapter, _backend_manager
    from billing.pg_ledger import PGBillingLedger as BL
    import db_adapter as db
    from backends.dispatcher import dispatch_job, backend_cancel_job, get_job_status

    _billing_ledger = BL()
    _db_adapter = db
    _backend_manager = {
        "dispatch": dispatch_job,
        "cancel": backend_cancel_job,
        "status": get_job_status,
    }
    logger.info("execution_worker initialized")


def sweep_orphans_once() -> int:
    """Снять с учёта running-джобы, которые пережили свой воркер."""
    if _db_adapter is None:
        return 0
    n = _db_adapter.sweep_orphan_running_jobs(JOB_ORPHAN_TIMEOUT_SECONDS)
    if n:
        logger.warning(
            "poll_and_execute.orphans_swept count=%s timeout=%ss",
            n,
            JOB_ORPHAN_TIMEOUT_SECONDS,
        )
    return n


def _job_cost_usd(duration_seconds) -> float:
    try:
        d = float(duration_seconds or 0.0)
    except (TypeError, ValueError):
        d = 0.0
    return round(max(d, 0.0) * 0.00001, 8)


def _tenant_plan(tenant_id: str) -> str:
    """План арендатора из БД (фолбэк 'free'). OT-82: не хардкодить free."""
    try:
        return (_db_adapter.get_tenant(tenant_id) or {}).get("plan", "free") or "free"
    except Exception:
        return "free"


def _mark_refund_pending(job_id: str, tenant_id: str, cost_usd: float, exc: Exception) -> None:
    """C10 fail-closed: refund не удался — durable-маркер + alert (не молча).

    DEBIT не должен остаться без возврата бесследно: маркер error=refund_pending
    делает ситуацию видимой и ретраибельной, alert поднимает её человеку/автоматике.
    """
    logger.error(
        "execute_and_bill.refund_failed_permanently job=%s tenant=%s amount=%.8f: %s",
        job_id,
        tenant_id,
        cost_usd,
        exc,
    )
    try:
        # C10: сумма возврата хранится В МАРКЕРЕ (cost_usd на строке в окне g5d часто 0).
        _db_adapter.update_execution_job(
            job_id,
            error=f"refund_pending:debit_exists:{cost_usd:.8f}",
            tenant_id=tenant_id,
        )
    except Exception as marker_exc:
        logger.error("execute_and_bill.refund_marker_failed job=%s: %s", job_id, marker_exc)
    try:
        from deps import alert_dispatcher
        from alerts import Alert, AlertLevel

        alert_dispatcher.send(
            Alert(
                # C10: деньги клиента — ERROR, не WARNING (fail-closed).
                level=AlertLevel.ERROR,
                title="💸 Refund pending (debit exists)",
                body=f"Job `{job_id}` tenant `{tenant_id}`: DEBIT ${cost_usd:.8f} без возврата — refund упал.",
                tags={"tenant_id": tenant_id, "job_id": job_id, "event": "refund_pending"},
            )
        )
    except Exception as alert_exc:
        logger.error("execute_and_bill.refund_alert_failed job=%s: %s", job_id, alert_exc)


def _parse_refund_amount(marker: str) -> float | None:
    """Извлечь сумму из маркера ``refund_pending:debit_exists:<сумма>`` (None если не парсится)."""
    prefix = "refund_pending:debit_exists:"
    if not marker or not marker.startswith(prefix):
        return None
    try:
        return float(marker[len(prefix):])
    except (TypeError, ValueError):
        return None


def _billing_pending_error(billing_status: str, exec_status: str | None) -> str:
    """Маркер ``billing:<статус>`` с ``:exec=<статус>`` для timeout/failed (свипер восстановит статус)."""
    marker = f"billing:{billing_status}"
    if exec_status in ("timeout", "failed"):
        marker += f":exec={exec_status}"
    return marker


def _billing_exec_status(error: str | None) -> str | None:
    """Извлечь исходный статус исполнения из маркера ``billing:...:exec=<статус>``."""
    if not error:
        return None
    needle = ":exec="
    idx = error.rfind(needle)
    if idx == -1:
        return None
    return error[idx + len(needle):] or None


def sweep_billing_pending_once(now: float | None = None) -> dict:
    """Повторно финализировать биллинг для billing_pending-джобов.

    Задание НЕ исполняется заново: только finalize_job_billing с тем же job_id.
    ok → completed с реальным cost_usd; pg_error/error/no_funds → остаётся
    billing_pending. Возвращает счётчики исходов.
    """
    from monitoring.metrics import roma_billing_pending_jobs, roma_billing_pending_sweep_total

    outcomes = {"ok": 0, "pg_error": 0, "error": 0, "no_funds": 0, "skip": 0, "deferred": 0, "mismatch": 0}
    if _db_adapter is None:
        return outcomes
    now = time.monotonic() if now is None else now
    try:
        roma_billing_pending_jobs.set(_db_adapter.count_billing_pending_jobs())
    except Exception as exc:
        logger.warning("billing_pending.count_failed: %s", exc)
    # OT-80: приостановленные no_funds-задачи отфильтровываем в SQL (до LIMIT),
    # чтобы не занимали слоты батча. Счётчик deferred считаем по paused_ids.
    paused_ids = [jid for jid, t in _no_funds_next_try.items() if t > now]
    outcomes["deferred"] = len(paused_ids)
    jobs = _db_adapter.get_billing_pending_jobs(limit=BILLING_PENDING_BATCH, exclude_ids=paused_ids)
    from main import finalize_job_billing

    for job in jobs:
        jid = job["id"]
        tid = job.get("tenant_id")
        try:
            status = finalize_job_billing(
                tid,
                jid,
                gpu_sec=float(job.get("duration_seconds") or 0.0),
                plan_name=_tenant_plan(tid),
                backend=job.get("backend"),
            )
        except PGUnavailableError as exc:
            logger.warning("billing_pending.pg_error job=%s: %s", jid, exc)
            status = "pg_error"
        except Exception as exc:
            logger.error("billing_pending.error job=%s: %s", jid, exc)
            status = "error"

        if status == "ok":
            _no_funds_next_try.pop(jid, None)
            try:
                # R1: восстановить исходный статус исполнения (timeout/failed), а не
                # ставить "completed" — иначе сбой выглядит успешным в счёте клиента.
                final_status = _billing_exec_status(job.get("error")) or "completed"
                _db_adapter.update_execution_job(
                    jid,
                    status=final_status,
                    cost_usd=_job_cost_usd(job.get("duration_seconds")),
                    error="",
                    tenant_id=tid,
                    if_status_not_in=_TERMINAL_STATUSES,
                )
            except PGUnavailableError as exc:
                # OT-81: биллинг сохранён, но финальный статус не записан — не
                # роняем весь свип; pg_error прерывает батч (см. ниже).
                logger.warning("billing_pending.complete_update.pg_error job=%s: %s", jid, exc)
                status = "pg_error"
            except Exception as exc:
                logger.error("billing_pending.complete_update.error job=%s: %s", jid, exc)
                status = "error"
            else:
                logger.info("billing_pending.billed job=%s tenant=%s", jid, tid)
        elif status == "no_funds":
            if jid not in _no_funds_next_try:
                logger.warning("billing_pending.no_funds job=%s tenant=%s", jid, tid)
            _no_funds_next_try[jid] = now + BILLING_PENDING_NO_FUNDS_RETRY_S
            # R1: сохранить исходный exec-статус в маркере no_funds.
            no_funds_marker = _billing_pending_error("no_funds", _billing_exec_status(job.get("error")))
            if job.get("error") != no_funds_marker:
                _db_adapter.update_execution_job(jid, error=no_funds_marker, tenant_id=tid)
        elif status == "mismatch":
            # R2: детерминированный конфликт ключа идемпотентности — пометить
            # терминально и не крутить в свипере (retry не разрешит).
            logger.error("billing_pending.mismatch job=%s tenant=%s", jid, tid)
            try:
                _db_adapter.update_execution_job(
                    jid, status="error", error="billing:mismatch", tenant_id=tid
                )
            except Exception as exc:
                logger.error("billing_pending.mismatch_update.error job=%s: %s", jid, exc)
        elif status not in outcomes:
            status = "error"
        outcomes[status] += 1
        if status == "pg_error":
            # OT-79: PG недоступен — не долбим оставшиеся задачи (каждая даёт
            # ~3s connection timeout и блокирует event loop).
            break
    if len(jobs) < BILLING_PENDING_BATCH:
        # Видели все pending: выкинуть бэкофф по джобам, которые ушли из очереди.
        seen = {j["id"] for j in jobs}
        for stale in [k for k in _no_funds_next_try if k not in seen]:
            _no_funds_next_try.pop(stale, None)
    for k, v in outcomes.items():
        if v:
            roma_billing_pending_sweep_total.labels(outcome=k).inc(v)
    return outcomes


def sweep_refund_pending_once() -> dict:
    """C10 (ВРЕМЕННЫЙ костыль до DDL): повторно провести refund для cancelled-джобов.

    Маркер refund_pending:debit_exists:<сумма> читается из error — любая другая
    запись в error его затрёт. До отдельного поля refund_required (DDL-раунд) это
    временный признак. refund идемпотентен по refund:<job_id> — повтор безопасен.
    """
    outcomes = {"ok": 0, "error": 0, "pg_error": 0}
    if _db_adapter is None:
        return outcomes
    try:
        jobs = _db_adapter.get_refund_pending_jobs(limit=50)
    except Exception as exc:
        logger.warning("refund_pending.list_failed: %s", exc)
        return outcomes
    from main import refund_job

    for job in jobs:
        jid = job["id"]
        tid = job.get("tenant_id")
        amount = _parse_refund_amount(job.get("error") or "")
        if amount is None:
            # Сумма не распарсилась — не возвращать 0 и не крутить вечно.
            logger.error("refund_pending.bad_marker job=%s error=%r", jid, job.get("error"))
            outcomes["error"] += 1
            continue
        try:
            refund_job(tid, amount, job_id=jid, reason="cancelled_after_debit")
            # успех → снять маркер (job остаётся cancelled).
            _db_adapter.update_execution_job(jid, error="", tenant_id=tid)
            outcomes["ok"] += 1
            logger.warning("refund_pending.refunded job=%s tenant=%s amount=%.8f", jid, tid, amount)
        except PGUnavailableError as exc:
            outcomes["pg_error"] += 1
            logger.warning("refund_pending.pg_error job=%s: %s", jid, exc)
            break
        except Exception as exc:
            outcomes["error"] += 1
            logger.error("refund_pending.error job=%s: %s", jid, exc)
    return outcomes


async def poll_and_execute():
    """Фоновый цикл: опрашивает БД на наличие queued-джобов и запускает execute_and_bill."""
    global _last_orphan_sweep, _last_billing_pending_sweep, _last_refund_pending_sweep
    logger.info("poll_and_execute started")
    while True:
        try:
            if time.monotonic() - _last_orphan_sweep >= _ORPHAN_SWEEP_INTERVAL_S:
                _last_orphan_sweep = time.monotonic()
                try:
                    sweep_orphans_once()
                except Exception as e:
                    logger.warning("poll_and_execute.orphan_sweep_error: %s", e)
            if time.monotonic() - _last_billing_pending_sweep >= _BILLING_PENDING_SWEEP_INTERVAL_S:
                _last_billing_pending_sweep = time.monotonic()
                try:
                    # OT-79: синхронный свипер снимаем с event loop (to_thread) —
                    # при недоступном PG каждый job давал бы ~3s блокировки.
                    await asyncio.to_thread(sweep_billing_pending_once)
                except Exception as e:
                    logger.warning("poll_and_execute.billing_pending_sweep_error: %s", e)
            if time.monotonic() - _last_refund_pending_sweep >= _REFUND_PENDING_SWEEP_INTERVAL_S:
                _last_refund_pending_sweep = time.monotonic()
                try:
                    # C10: refund_pending-свипер снимаем с event loop (to_thread),
                    # как billing_pending (OT-79).
                    await asyncio.to_thread(sweep_refund_pending_once)
                except Exception as e:
                    logger.warning("poll_and_execute.refund_pending_sweep_error: %s", e)
            # Атомарный claim: queued → running уже выполнен в БД, и вернулись
            # только джобы, захваченные ЭТОЙ репликой (P1: двойной захват).
            jobs = _db_adapter.claim_queued_execution_jobs(limit=5)
            for job in jobs:
                jid = job["id"]
                tid = job["tenant_id"]
                payload = job.get("payload") or {}
                _track(asyncio.ensure_future(execute_and_bill(jid, tid, payload)))
                logger.info("poll_and_execute.started job=%s tenant=%s", jid, tid)
        except Exception as e:
            logger.warning("poll_and_execute.error: %s", e)
        await asyncio.sleep(5)


def dispatch_is_ready(backend_name: str | None, status: str | None) -> bool:
    return status in ("running", "provisioning") or (backend_name == "local" and status == "queued")


async def _execute_and_bill_impl(
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
        result = await _backend_manager["dispatch"](job_id=job_id, tenant_id=tenant_id, payload=payload)
        # The dispatched backend comes ONLY from the dispatch result. payload.backend
        # is the *requested* backend (kept in the log below) and must not decide
        # failed-vs-running nor be persisted as the actual backend.
        backend_name = result.get("backend")
        _price_per_hour = result.get("price_per_hour", 0.0)
        contract_id = result.get("contract_id")
        status = result.get("status")

        # Fail fast when there is nothing to execute: dispatch returned failed/error,
        # or the result carries no backend (empty fallback chain). Do not fall through
        # to "running" + a 120s poll loop and do not substitute backend="local".
        if status in ("failed", "error") or not backend_name:
            logger.warning(
                "execute_and_bill.dispatch_failed job=%s requested_backend=%s: %s",
                job_id,
                payload.get("backend"),
                result.get("message", ""),
            )
            _db_adapter.update_execution_job(
                job_id,
                status="failed",
                backend=backend_name,
                completed_at=datetime.now(timezone.utc).isoformat(),
                error=str(result.get("message", "dispatch failed"))[:500],
                tenant_id=tenant_id,
            )
            return {
                "status": "failed",
                "job_id": job_id,
                "error": result.get("message", ""),
            }

        # Only mark "running" and poll when dispatch actually accepted the job.
        # queued/timeout/unknown are not ready — don't burn 120s polling them.
        ready = dispatch_is_ready(backend_name, status)
        if not ready:
            logger.warning(
                "execute_and_bill.dispatch_not_ready job=%s backend=%s status=%s",
                job_id,
                backend_name,
                status,
            )
            _db_adapter.update_execution_job(
                job_id,
                status="failed",
                backend=backend_name,
                completed_at=datetime.now(timezone.utc).isoformat(),
                error=f"dispatch not ready (status={status})",
                tenant_id=tenant_id,
            )
            return {
                "status": "failed",
                "job_id": job_id,
                "error": f"dispatch not ready (status={status})",
            }

        # Overwrite the client-requested backend with the actually-dispatched one so
        # downstream (poll loop / _execute_command) never sees payload["backend"].
        payload["backend"] = backend_name
        backend_job_id = result.get("backend_job_id")
        if backend_job_id is None and contract_id is not None:
            backend_job_id = str(contract_id)
        if backend_job_id is not None:
            payload["backend_job_id"] = backend_job_id

        _db_adapter.update_execution_job(
            job_id,
            status="running",
            backend=backend_name,
            backend_job_id=backend_job_id,
            tenant_id=tenant_id,
        )
    except Exception as exc:
        logger.error("execute_and_bill.dispatch_failed job=%s: %s", job_id, exc)
        _db_adapter.update_execution_job(
            job_id,
            status="failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc)[:500],
            tenant_id=tenant_id,
        )
        return {"status": "failed", "error": str(exc)}

    # Шаг 2: Poll до завершения (макс 10 мин для GPU, 2 мин для local)
    max_wait = 600 if backend_name == "vastai" else 120
    poll_interval = 15 if backend_name == "vastai" else 5
    waited = 0.0
    status = "unknown"

    while waited < max_wait:
        try:
            status_info = await _backend_manager["status"](job_id, backend_job_id)
            status = status_info.get("status", "unknown")

            if status in (
                "completed",
                "failed",
                "error",
                "stopped",
                "cancelled",
                "destroyed",
            ):
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

    # OT-99: фиксируем биллингуемое время ДО транзакции и храним то же самое в
    # duration_seconds — свипер при повторе после неопределённого COMMIT обязан
    # дать ту же сумму (иначе сверка по ledger_id разойдётся).
    elapsed = round(time.monotonic() - start_time, 2)
    now_iso = datetime.now(timezone.utc).isoformat()
    cost_usd = round(elapsed * 0.00001, 8)

    billing_status = "skip"
    try:
        from main import finalize_job_billing

        billing_status = finalize_job_billing(
            tenant_id,
            job_id,
            gpu_sec=elapsed,
            plan_name=_tenant_plan(tenant_id),
            backend=backend_name,
        )
        logger.info(
            "execute_and_bill.finalized tenant=%s job=%s gpu_sec=%.1f status=%s",
            tenant_id,
            job_id,
            elapsed,
            billing_status,
        )
    except PGUnavailableError as exc:
        # fail-closed: деньги НЕ сохранились — не помечаем billed, не зануляем молча.
        logger.error("execute_and_bill.billing_pg_error job=%s: %s", job_id, exc)
        billing_status = "pg_error"
    except Exception as exc:
        logger.error("execute_and_bill.billing_error job=%s: %s", job_id, exc)
        billing_status = "error"

    billed_ok = billing_status == "ok"
    write_status = status
    if billing_status == "mismatch":
        # R2: детерминированный конфликт ключа идемпотентности — retry не разрешит,
        # не отдаём свиперу на повторный дебет (иначе вечный billing_pending).
        write_status = "error"
    elif billing_status in ("pg_error", "error", "no_funds") and status in ("completed", "timeout", "failed"):
        # R1 (caveat 3): usage-запись не сохранилась (pg_error/error) или средств нет
        # (no_funds) — retry через billing_pending свипер. skip (0-cost/уже финал) сюда не идёт.
        write_status = "billing_pending"

    # G5 (CAS): пока задача исполнялась, её могли отменить (/cancel) или закрыть
    # (/complete). Слепой UPDATE перезаписал бы этот статус, а billing_pending ещё
    # и отдал бы отменённую задачу свиперу на DEBIT. Пишем только из
    # нетерминального статуса; 0 строк → только лог, без дебета.
    n = _db_adapter.update_execution_job(
        job_id,
        status=write_status,
        completed_at=now_iso,
        cost_usd=cost_usd if billed_ok else 0.0,
        duration_seconds=round(elapsed, 2),
        error="" if billed_ok else _billing_pending_error(billing_status, status),
        tenant_id=tenant_id,
        if_status_not_in=_FINAL_WRITE_BLOCKED_STATUSES,
    )
    if n == 0:
        if billed_ok:
            # C10 (g5d): деньги списаны, но финальная запись не применилась — задачу
            # отменили между дебетом и финальным UPDATE. Вернуть деньги (возврат
            # без запроса клиента), идемпотентно по refund:<job_id>.
            cur = _db_adapter.get_execution_job(job_id)
            if cur and cur.get("tenant_id") == tenant_id and cur.get("status") == "cancelled":
                # C10 fail-closed (retry + компенсация): refund не должен молча
                # оставлять DEBIT. Retry идемпотентен по refund:<job_id>.
                try:
                    from main import refund_job

                    last_exc = None
                    for attempt in range(3):
                        try:
                            refund_job(tenant_id, cost_usd, job_id=job_id, reason="cancelled_after_debit")
                            logger.warning(
                                "execute_and_bill.refunded_cancelled job=%s tenant=%s amount=%.8f",
                                job_id,
                                tenant_id,
                                cost_usd,
                            )
                            break
                        except Exception as exc:
                            last_exc = exc
                            logger.warning(
                                "execute_and_bill.refund_attempt_failed attempt=%d job=%s: %s",
                                attempt + 1,
                                job_id,
                                exc,
                            )
                            await asyncio.sleep(0.5 * (attempt + 1))
                    else:
                        # C10 fail-closed: исчерпаны попытки — durable-маркер + alert.
                        _mark_refund_pending(job_id, tenant_id, cost_usd, last_exc)
                except Exception as exc:
                    # Даже import/цикл упал — не молча.
                    _mark_refund_pending(job_id, tenant_id, cost_usd, exc)
        (logger.error if billed_ok else logger.warning)(
            "execute_and_bill.final_update_skipped job=%s tenant=%s worker_status=%s billing=%s: "
            "статус уже изменён другим путём, финальная запись не применена",
            job_id,
            tenant_id,
            write_status,
            billing_status,
        )

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


async def execute_and_bill(job_id: str, tenant_id: str, payload: dict) -> dict:
    """Врапер: гарантирует терминальный статус + cleanup backend при отмене (drain_inflight)."""
    try:
        return await _execute_and_bill_impl(job_id, tenant_id, payload)
    except asyncio.CancelledError:
        logger.warning("execute_and_bill.cancelled job=%s tenant=%s", job_id, tenant_id)
        try:
            _db_adapter.update_execution_job(
                job_id,
                status="cancelled",
                completed_at=datetime.now(timezone.utc).isoformat(),
                error="cancelled by drain_inflight",
                tenant_id=tenant_id,
                if_status_not_in=_FINAL_WRITE_BLOCKED_STATUSES,
            )
        except Exception:
            pass
        try:
            if _backend_manager:
                await asyncio.shield(_backend_manager["cancel"](job_id, tenant_id))
        except Exception:
            pass
        raise


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
    return _track(loop.create_task(execute_and_bill(job_id, tenant_id, payload)))
