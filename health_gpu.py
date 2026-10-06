#!/usr/bin/env python3
"""GPU availability check for ROMA health endpoint."""

import os
import subprocess
import logging
from typing import Dict

logger = logging.getLogger("roma.gpu")


def check_gpu_available() -> Dict[str, object]:
    result: Dict[str, object] = {
        "gpu_available": False,
        "gpu_check_reason": "no /dev/nvidia* devices",
    }
    try:
        if os.path.exists("/dev/nvidia0"):
            try:
                subprocess.run(
                    ["nvidia-smi"], capture_output=True, timeout=5, check=True
                )
                result["gpu_available"] = True
                result["gpu_check_reason"] = ""
            except FileNotFoundError:
                result["gpu_check_reason"] = "nvidia-smi not found"
            except subprocess.TimeoutExpired:
                result["gpu_check_reason"] = "nvidia-smi timeout"
            except subprocess.CalledProcessError as e:
                result["gpu_check_reason"] = f"nvidia-smi error: {e.returncode}"
        else:
            result["gpu_check_reason"] = "/dev/nvidia0 not found"
    except Exception as e:
        logger.warning("GPU check failed: %s", e)
        result["gpu_check_reason"] = str(e)
    return result
