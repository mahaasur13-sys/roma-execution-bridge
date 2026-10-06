"""ROMA Cost Explainability Engine — Why this costs what it costs."""

from cost.predictor import CostPredictor
from plugins.plugin_api import PluginCapability

GPU_RATE = 0.000086  # $ per GPU-second (PRO tier)


class CostExplainabilityEngine:
    @staticmethod
    def _money(value) -> str:
        """Цена может быть не установлена (нет записи клиента) — печать честная."""
        return "n/a" if value is None else f"{value:.2f}"

    def explain(self, task: str, tenant_id: str | None = None) -> dict:
        gpu_required = "gpu" in task.lower() or "train" in task.lower()
        # G-PRICING-TIER-PATH: тариф — из записи клиента; без tenant_id решения
        # о цене нет (предиктор отказывает кодом UNKNOWN_TENANT).
        pred = CostPredictor().predict(
            task,
            gpu_required=gpu_required,
            plugin_type="ml_training",
            tenant_id=tenant_id,
        )
        bd = pred["breakdown"]
        gpu_seconds = bd.get("gpu_seconds", 3600)
        gpu_count = 1 if gpu_required else 0
        gpu_cost = gpu_seconds * GPU_RATE * gpu_count
        plugin_info = {
            "name": "ml_training",
            "capabilities": [PluginCapability.GPU_ENABLED],
        }
        alternatives = self._generate_alternatives(pred, gpu_cost)
        reasons = self._decision_reasons(task, pred, plugin_info)
        return {
            "execution_plan": self._plan_steps(task, plugin_info, pred, gpu_seconds),
            "cost_breakdown": {
                "GPU time": round(gpu_cost, 4),
                "Queue": round(bd.get("queue", 0.0), 4),
                "Storage": round(bd.get("storage", 0.0), 4),
                "Overhead": round(bd.get("overhead", 0.0), 4),
            },
            "total_cost": (
                None
                if pred["estimated_cost"] is None
                else round(pred["estimated_cost"], 4)
            ),
            "decision": pred.get("decision"),
            "alternatives": alternatives,
            "decision_reasons": reasons,
            "plugin_used": plugin_info["name"],
        }

    def _generate_alternatives(self, pred: dict, gpu_cost: float) -> list:
        base = (
            gpu_cost
            + sum(
                pred["breakdown"].get(k, 0.0) for k in ["queue", "storage", "overhead"]
            )
            or 0.01
        )
        return [
            {
                "description": "CPU cluster (no GPU)",
                "cost": round(base * 0.32, 4),
                "savings": "68",
            },
            {
                "description": "Smaller dataset (50%)",
                "cost": round(base * 0.64, 4),
                "savings": "36",
            },
            {
                "description": "Off-peak scheduling",
                "cost": round(base * 0.85, 4),
                "savings": "15",
            },
        ]

    def _decision_reasons(self, task: str, pred: dict, plugin: dict) -> list:
        reasons = [f"Task requires GPU (detected from: {task})"]
        if pred.get("risk_flags"):
            reasons.append(f"Risk flags: {', '.join(pred['risk_flags'])}")
        reasons.append(f"Plugin: {plugin['name']} (GPU_ENABLED capability)")
        reasons.append(
            f"Cost estimate: ${self._money(pred.get('estimated_cost'))} "
            f"(tier: {pred.get('tier') or 'не установлен'})"
        )
        return reasons

    def _plan_steps(
        self, task: str, plugin: dict, pred: dict, gpu_seconds: float
    ) -> list:
        return [
            {"phase": "validation", "description": "Input contract + security gate"},
            {
                "phase": "cost_check",
                "description": f"Estimate: ${self._money(pred.get('estimated_cost'))}",
            },
            {"phase": "plugin_load", "description": f"Load {plugin['name']} plugin"},
            {"phase": "scheduling", "description": f"Duration ~{gpu_seconds/60:.0f}m"},
            {
                "phase": "execution",
                "description": f"GPU node: {pred.get('decision', 'APPROVED')}",
            },
            {
                "phase": "completion",
                "description": "Event store + billing ledger update",
            },
        ]
