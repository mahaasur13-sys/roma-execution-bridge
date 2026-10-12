"""RaaS-04: cost estimation functions used by the admission guard."""

import pytest

from cost.gate import estimate_cluster_cost, estimate_cost


def test_estimate_cost_gpu():
    assert estimate_cost({"gpu_required": True}) == 0.002


def test_estimate_cost_cpu():
    assert estimate_cost({"gpu_required": False}) == 0.0001


def test_estimate_cluster_cost_nodes_times_seconds():
    assert estimate_cluster_cost({"nodes": 2, "expected_gpu_seconds": 100}) == 0.002


def test_estimate_cluster_cost_missing_fields_raise():
    with pytest.raises(ValueError):
        estimate_cluster_cost({})
    with pytest.raises(ValueError):
        estimate_cluster_cost({"nodes": 2})


def test_estimate_cluster_cost_below_one_raises():
    with pytest.raises(ValueError):
        estimate_cluster_cost({"nodes": 0, "expected_gpu_seconds": 100})
    with pytest.raises(ValueError):
        estimate_cluster_cost({"nodes": 1, "expected_gpu_seconds": 0})
