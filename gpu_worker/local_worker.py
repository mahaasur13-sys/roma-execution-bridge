#!/usr/bin/env python3
"""ROMA Local Worker — финальная версия с памятью сессии"""
import time, json, subprocess, urllib.request, urllib.error

API_BASE = "http://localhost:8900"
API_KEY = "test-key-12345"
POLL_INTERVAL = 3
processed_ids = set()

def api_get(path):
    req = urllib.request.Request(f"{API_BASE}{path}", headers={"X-API-Key": API_KEY})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

def run_cmd(task):
    try:
        r = subprocess.run(task, shell=True, capture_output=True, text=True, timeout=300)
        return {"ok": r.returncode == 0, "out": r.stdout, "err": r.stderr, "code": r.returncode}
    except Exception as e:
        return {"ok": False, "error": str(e)}

print("🚀 ROMA Local Worker запущен")
print(f"   API: {API_BASE} | Ключ: {API_KEY} | Интервал: {POLL_INTERVAL}с")
print("-" * 60)

while True:
    try:
        jobs = api_get("/jobs").get("jobs", [])
        queued = [j for j in jobs if j.get("status") == "queued" and j["job_id"] not in processed_ids]
        if not queued:
            print(f"[{time.strftime('%H:%M:%S')}] Очередь пуста (обработано: {len(processed_ids)})")
            time.sleep(POLL_INTERVAL)
            continue

        queued.sort(key=lambda j: (-j.get("priority", 5), j.get("submitted_at", "")))
        job = queued[0]
        jid, payload = job["job_id"], job.get("payload", {})
        task, mode, pri = payload.get("task", ""), payload.get("execution_mode", "?"), payload.get("priority", 5)
        print(f"\n📥 {jid[:8]}... | приоритет={pri} | режим={mode}")
        print(f"   Команда: {task[:65]}{'...' if len(task)>65 else ''}")

        if not task or task[0].isspace() or all(c.isalpha() or c.isspace() for c in task.split()[0] if task.split()):
            first = task.split()[0].lower() if task.split() else ""
            known = {'echo','date','ls','cat','uname','nvidia-smi','python','python3','curl','wget',
                     'git','docker','kubectl','mkdir','rm','cp','mv','find','grep','awk','sed',
                     'ps','top','df','du','free','ping','whoami','id','pwd','clear','exit',
                     'bash','sh','zsh','htop','neofetch','lscpu','lsmem','lsusb','lspci'}
            has_shell = any(c in task for c in '";|&`$()[]{}<>!#=\\')
            if first not in known and not has_shell and '/' not in task:
                print(f"   ⏭️  Пропущено: не команда")
                processed_ids.add(jid)
                print("-" * 60)
                continue

        result = run_cmd(task)
        processed_ids.add(jid)
        if result["ok"]:
            print(f"   ✅ Успех (код {result['code']})")
            for line in result["out"].strip().split("\n")[:6]:
                print(f"      📤 {line[:90]}")
            if len(result["out"].strip().split("\n")) > 6:
                print(f"      ...")
        else:
            print(f"   ❌ Ошибка: {result.get('error', result.get('err','?'))[:100]}")
        print("-" * 60)
    except urllib.error.URLError as e:
        print(f"❌ Нет связи с API: {e.reason}")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    time.sleep(POLL_INTERVAL)
