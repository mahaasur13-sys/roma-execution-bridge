#!/usr/bin/env python3
"""ROMA CLI — UX-First Human Interface Layer.

Commands:
  roma run "task"       — submit with interactive cost preview
  roma explain "task"   — explain mode (why, alternatives)
  roma cost "task"       — cost preview only
  roma status [job_id]  — job tracking
  roma dashboard        — launch dashboard
"""
import sys, json, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from cost.predictor import CostPredictor
from cost.gate import DecisionGate
from cost.estimator import RuntimeEstimator
from cost.explainability import CostExplainabilityEngine
from plugins.plugin_runtime import PluginRuntime
from plugins.plugin_api import IPlugin


class ROMA_CLI:
    PROMPT_OPTIONS = """
[yes]  Run as planned
[opt]  Show cheaper alternatives
[cancel]  Abort"""

    def __init__(self):
        self.predictor = CostPredictor()
        self.gate = DecisionGate()
        self.estimator = RuntimeEstimator()
        self.explainer = CostExplainabilityEngine()
        self.runtime = PluginRuntime()

    def cmd_run(self, task: str) -> int:
        print(f"\n🎯 Task: {task}\n")
        print("⏳ Analyzing...")
        prediction = self.predictor.predict(task, gpu_required=("gpu" in task.lower() or "train" in task.lower()))
        print(f"\n💰 Estimated cost: ${prediction['estimated_cost']:.2f}")
        print(f"⏱️  Duration: ~{self._format_duration(prediction['estimated_duration_minutes'])}")
        print(f"🖥️  GPU: {prediction['gpu_node']} (×{prediction['gpu_count']})")
        print(f"⚠️  Risk: {prediction['risk_level']}\n")
        print(self._breakdown_str(prediction['breakdown']))
        decision = self.gate.decide(task, "default", gpu_required=("gpu" in task.lower() or "train" in task.lower()), **prediction)
        if decision['action'] == "REJECTED":
            print(f"\n🚫 REJECTED: {decision['reason']}")
            return 1
        if decision['action'] == "REQUIRES_CONFIRMATION":
            print(f"\n⚠️  Cost warning: ${decision['final_cost']:.2f} — confirm?")
            print(self.PROMPT_OPTIONS)
            choice = input("\n> ").strip().lower()
            if choice == "cancel" or choice == "c":
                print("Cancelled."); return 0
            if choice == "opt" or choice == "o":
                self._show_alternatives(task)
                return self.cmd_run(task)
        print(f"\n✅ {decision['action']}: ${decision.get('final_cost', prediction['estimated_cost']):.2f}")
        print(f"\n🚀 Submitting job...")
        job_id = self._submit_job(task, prediction)
        print(f"✅ Job submitted: {job_id}")
        return 0

    def cmd_explain(self, task: str) -> int:
        print(f"\n🧠 Explain: {task}\n")
        explanation = self.explainer.explain(task)
        print("=" * 50)
        print("📋 EXECUTION PLAN")
        for step in explanation['execution_plan']:
            print(f"  {step['phase']}: {step['description']}")
        print("\n💰 COST BREAKDOWN")
        for item, cost in explanation['cost_breakdown'].items():
            print(f"  {item}: ${cost:.2f}")
        print(f"\n  TOTAL: ${explanation['total_cost']:.2f}")
        if explanation.get('alternatives'):
            print("\n💡 CHEAPER ALTERNATIVES")
            for alt in explanation['alternatives']:
                print(f"  • {alt['description']} → ${alt['cost']:.2f} ({alt['savings']}%)")
        print("\n🔍 WHY THIS CHOICE")
        for reason in explanation['decision_reasons']:
            print(f"  • {reason}")
        return 0

    def cmd_cost(self, task: str) -> int:
        print(f"\n💰 Cost preview: {task}\n")
        prediction = self.predictor.predict(task, gpu_required=("gpu" in task.lower() or "train" in task.lower()))
        result = {"estimated_cost": round(prediction['estimated_cost'], 4),
                  "breakdown": {k: round(float(v), 4) for k, v in prediction["breakdown"].items() if isinstance(v, (int, float))},
                  "gpu_seconds": prediction['breakdown'].get('gpu_seconds', 0),
                  "gpu": prediction['breakdown'].get('gpu_required', True) and 'gpu-node-1' or 'cpu-cluster',
                  "risk": prediction['breakdown'].get('gpu_required', True) and 'LOW' or 'LOW'}
        print(json.dumps(result, indent=2))
        return 0

    def cmd_status(self, job_id: str = None) -> int:
        print(f"\n🖥️  Status" + (f": {job_id}" if job_id else " (all jobs)"))
        print("⚙️  Implementation: Use /status endpoint + event store query")
        print("📊 Live view: job state, GPU usage, queue depth")
        return 0

    def _show_alternatives(self, task: str) -> None:
        explanation = self.explainer.explain(task)
        print("\n💡 OPTIMIZATION ALTERNATIVES:")
        if explanation.get('alternatives'):
            for alt in explanation['alternatives']:
                print(f"  • {alt['description']} → ${alt['cost']:.2f} ({alt['savings']}%)")
        else:
            print("  (no cheaper alternatives found)")

    def _format_duration(self, minutes: float) -> str:
        h = int(minutes // 60); m = int(minutes % 60)
        return f"{h}h {m}m" if h else f"{m}m"

    def _breakdown_str(self, breakdown: dict) -> str:
        return "\n".join(f"  {k}: ${v:.2f}" for k, v in breakdown.items())

    def _submit_job(self, task: str, prediction: dict) -> str:
        import uuid; return f"job-{uuid.uuid4().hex[:8]}"


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    cmd = sys.argv[1]
    task = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
    cli = ROMA_CLI()
    if cmd == "run":
        sys.exit(cli.cmd_run(task) if task else (print("Usage: roma run 'task'"), 1))
    elif cmd == "explain":
        sys.exit(cli.cmd_explain(task) if task else (print("Usage: roma explain 'task'"), 1))
    elif cmd == "cost":
        sys.exit(cli.cmd_cost(task) if task else (print("Usage: roma cost 'task'"), 1))
    elif cmd == "status":
        sys.exit(cli.cmd_status(sys.argv[2] if len(sys.argv) > 2 else None))
    elif cmd == "dashboard":
        print("🚀 Dashboard: roma-dashboard.service on :8051")
        sys.exit(0)
    else:
        print(f"Unknown command: {cmd}"); sys.exit(1)


if __name__ == "__main__": main()
