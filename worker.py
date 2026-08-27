#!/usr/bin/env python3
"""ROMA Local Worker — простой воркер для выполнения задач из очереди API (hardened)."""
import time
import json
import os
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from safe_exec import run_command_checked, CommandNotAllowed  # noqa: E402

API_BASE = "http://localhost:8900"
API_KEY = "test-key-12345"
POLL_INTERVAL = 5

def api_get(path):
    req = urllib.request.Request(f"{API_BASE}{path}", headers={"X-API-Key": API_KEY})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

def run_job(task_cmd):
    try:
        r = run_command_checked(task_cmd)
        return {"success": r["ok"], "stdout": r["out"], "stderr": r["err"], "returncode": r["code"]}
    except CommandNotAllowed as e:
        return {"success": False, "error": str(e), "stdout": "", "stderr": "", "returncode": 126}
    except Exception as e:
        return {"success": False, "error": str(e)}

def main():
    print("ROMA Local Worker launched (hardened: whitelist, no shell)")
    print(f"   API: {API_BASE}")
    print(f"   Interval: {POLL_INTERVAL}s")
    while True:
        try:
            data = api_get("/jobs")
            job_list = data.get("jobs", [])
            queued = [j for j in job_list if j.get("status") == "queued"]
            if not queued:
                print(f"[{time.strftime('%H:%M:%S')}] Queue empty, waiting...")
                time.sleep(POLL_INTERVAL)
                continue
            job = queued[0]
            job_id = job["job_id"]
            payload = job.get("payload", {})
            task = payload.get("task", "")
            print(f"\nJob {job_id} | task: {task[:80]}")
            result = run_job(task)
            status_icon = "OK" if result["success"] else "FAIL"
            print(f"   {status_icon}: {result.get('stdout','')[:150]}")
            if not result["success"]:
                print(f"   rejected: {result.get('error','')[:150]}")
        except urllib.error.URLError as e:
            print(f"API error: {e.reason}")
        except Exception as e:
            print(f"Worker error: {e}")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
