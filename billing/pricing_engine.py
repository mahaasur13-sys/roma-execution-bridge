#!/usr/bin/env python3
"""ROMA Pricing Engine — Dynamic pricing, tier management, cost models."""

from enum import Enum


class PricingTier(Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class TaskCategory(Enum):
    CV_TRAINING = "cv_training"
    CV_INFERENCE = "cv_inference"
    LLM_INFERENCE = "llm_inference"
    LLM_TRAINING = "llm_training"
    IMAGE_GEN = "image_gen"
    DATA_PROCESSING = "data_processing"
    DEFAULT = "default"


TASK_MULTIPLIERS = {
    TaskCategory.CV_TRAINING: 4,
    TaskCategory.CV_INFERENCE: 2,
    TaskCategory.LLM_INFERENCE: 8,
    TaskCategory.LLM_TRAINING: 10,
    TaskCategory.IMAGE_GEN: 6,
    TaskCategory.DATA_PROCESSING: 3,
    TaskCategory.DEFAULT: 1,
}


class TaskCategory(Enum):
    CV_TRAINING = "cv_training"
    LLM_INFERENCE = "llm_inference"
    IMAGE_GEN = "image_gen"
    DEFAULT = "default"


class PricingEngine:
    def __init__(self):
        self.utilization = 0.5
        self.multiplier = 1.0

    def set_utilization(self, util: float):
        self.utilization = util
        self.multiplier = 1.0 + (util * 0.5)

    def calculate(
        self, tier: PricingTier, gpu_s: float = 0, cpu_s: float = 0, gb_s: float = 0
    ) -> dict:
        rates = {
            PricingTier.FREE: (0.0, 0.0, 0.0),
            PricingTier.PRO: (0.005, 0.001, 0.00001),
            PricingTier.ENTERPRISE: (0.004, 0.0005, 0.000005),
        }
        gpu_rate, cpu_rate, gb_rate = rates.get(tier, rates[PricingTier.FREE])
        gpu_cost = (gpu_s / 3600) * gpu_rate * self.multiplier
        cpu_cost = (cpu_s / 3600) * cpu_rate
        gb_cost = (gb_s / 3600) * gb_rate
        return {
            "tier": tier.value,
            "gpu_cost": round(gpu_cost, 6),
            "cpu_cost": round(cpu_cost, 6),
            "storage_cost": round(gb_cost, 6),
            "total": round(gpu_cost + cpu_cost + gb_cost, 6),
            "currency": "USD",
        }

    def classify_task(self, task: str) -> TaskCategory:
        t = task.lower()
        if any(kw in t for kw in ["train", "fine-tune", "finetune"]):
            if any(kw in t for kw in ["llm", "gpt", "bert", "transformer", "llama"]):
                return TaskCategory.LLM_TRAINING
            return TaskCategory.CV_TRAINING
        if any(kw in t for kw in ["yolo", "detection", "inference", "predict"]):
            if any(kw in t for kw in ["llm", "gpt", "bert", "transformer", "llama"]):
                return TaskCategory.LLM_INFERENCE
            return TaskCategory.CV_INFERENCE
        if any(kw in t for kw in ["stable", "diffusion", "image gen", "dalle"]):
            return TaskCategory.IMAGE_GEN
        if any(kw in t for kw in ["etl", "data", "processing", "transform"]):
            return TaskCategory.DATA_PROCESSING
        return TaskCategory.DEFAULT

    def estimate_duration(self, task: str, gpu_type: str) -> int:
        base_map = {"RTX3060": 1800, "RTX4090": 900, "A100": 300, "H100": 150}
        base = base_map.get(gpu_type, 900)
        category = self.classify_task(task)
        multiplier = TASK_MULTIPLIERS.get(category, 1)
        return base * multiplier


def estimate_cost(
    tenant_id: str, gpu_seconds: float, cpu_seconds: float, gb_seconds: float
) -> float:
    pe = PricingEngine()
    result = pe.calculate(
        PricingTier.PRO, gpu_s=gpu_seconds, cpu_s=cpu_seconds, gb_s=gb_seconds
    )
    return result["total"]
