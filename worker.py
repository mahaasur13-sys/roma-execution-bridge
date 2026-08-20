#!/usr/bin/env python3
"""ROMA Local Worker — простой воркер для выполнения задач из очереди API"""
import time, json, subprocess, urllib.request, urllib.error

API_BASE = "http://localhost:8900"
API_KEY = "test-key-12345"
POLL_INTERVAL = 5

def api_get(path):
    req = urllib.request.Request(f"{API_BASE}{path}", headers={"X-API-Key": API_KEY})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

def run_job(task_cmd):
    try:
        result = subprocess.run(task_cmd, shell=True, capture_output=True, text=True, timeout=300)
        return {"success": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Timeout after 300s"}
    except Exception as e:
        return {"success": False, "error": str(e)}

def main():
    print("ROMA Local Worker launched")
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
        except urllib.error.URLError as e:
            print(f"API error: {e.reason}")
        except Exception as e:
            print(f"Worker error: {e}")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
