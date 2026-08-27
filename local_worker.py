#!/usr/bin/env python3
"""ROMA Local Worker — финальная версия с памятью сессии (hardened)."""
import time
import json
import os
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from safe_exec import run_command_checked, CommandNotAllowed  # noqa: E402

API_BASE = "http://localhost:8900"
API_KEY = "admin-key-beta-2026"
POLL_INTERVAL = 3
processed_ids = set()

def api_get(path):
    req = urllib.request.Request(f"{API_BASE}{path}", headers={"X-API-Key": API_KEY})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

def api_post(path):
    req = urllib.request.Request(
        f"{API_BASE}{path}", method="POST", data=b"{}",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

def run_cmd(task):
    try:
        r = run_command_checked(task)
        return {"ok": r["ok"], "out": r["out"], "err": r["err"], "code": r["code"]}
    except CommandNotAllowed as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

print("🚀 ROMA Local Worker запущен (hardened: whitelist, без shell)")
print(f"   API: {API_BASE} | Ключ: {API_KEY} | Интервал: {POLL_INTERVAL}с")
print("-" * 60)

while True:
    try:
        jobs = api_get("/jobs").get("jobs", [])
        # local-задачи: backend=local, статус queued/running, ещё не завершены
        pending = [j for j in jobs
                   if j.get("backend") == "local"
                   and j.get("status") in ("queued", "running")
                   and not j.get("completed_at")
                   and (j.get("job_id") or j.get("id")) not in processed_ids]
        if not pending:
            print(f"[{time.strftime('%H:%M:%S')}] Очередь пуста (обработано: {len(processed_ids)})")
            time.sleep(POLL_INTERVAL)
            continue

        pending.sort(key=lambda j: (-j.get("priority", 5), j.get("submitted_at", "")))
        job = pending[0]
        jid = job.get("job_id") or job.get("id")
        payload = job.get("payload", {})
        task, mode, pri = payload.get("task", ""), payload.get("execution_mode", "?"), payload.get("priority", 5)
        print(f"\n📥 {jid[:8]}... | статус={job.get('status')} | приоритет={pri} | режим={mode}")
        print(f"   Команда: {task[:65]}{'...' if len(task)>65 else ''}")

        result = run_cmd(task)
        processed_ids.add(jid)
        if result["ok"]:
            print(f"   ✅ Успех (код {result['code']})")
            for line in result["out"].strip().split("\n")[:6]:
                print(f"      📤 {line[:90]}")
            if len(result["out"].strip().split("\n")) > 6:
                print("      ...")
            try:
                api_post(f"/complete/{jid}")
                print("   📬 POST /complete отправлен")
            except Exception as e:
                print(f"   ⚠️ /complete не удался: {e}")
        else:
            print(f"   ❌ Отклонено/Ошибка: {result.get('error', result.get('err','?'))[:100]}")
        print("-" * 60)
    except urllib.error.URLError as e:
        print(f"❌ Нет связи с API: {e.reason}")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    time.sleep(POLL_INTERVAL)
