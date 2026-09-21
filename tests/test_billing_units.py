"""N-BLACK-B: поведение денежных единиц биллинга — тарифы, агрегация, счета.

Зачем: billing/metering.py, billing/invoicing.py, billing/aggregator.py и
billing/models/billing_events.py — денежный путь (money path), но не исполнялись
ни одним тестом. Здесь проверяются ставки, накопление по тенантам и окно выборки —
то есть арифметика, из которой складывается счёт клиенту.
"""

import time

import pytest

from billing.aggregator import UsageAggregator, simulate_usage
from billing.invoicing import InvoicingEngine
from billing.metering import CPU_RATE, GPU_RATE, RAM_RATE, MeteringEngine, UsageEvent
from billing.models.billing_events import (
    BillingEvent,
    BillingEventStore,
    BillingEventType,
)


class TestUsageEventRates:
    def test_cost_is_derived_from_event_type(self):
        assert UsageEvent("t", "gpu_usage", 1000.0, "j").cost_usd == pytest.approx(
            1000.0 * GPU_RATE
        )
        assert UsageEvent("t", "cpu_usage", 1000.0, "j").cost_usd == pytest.approx(
            1000.0 * CPU_RATE
        )
        assert UsageEvent("t", "storage_usage", 1000.0, "j").cost_usd == pytest.approx(
            1000.0 * RAM_RATE
        )
        assert UsageEvent("t", "plugin_exec", 3, "j").cost_usd == pytest.approx(
            3 * 0.001
        )

    def test_unknown_event_type_costs_nothing(self):
        assert UsageEvent("t", "mystery_usage", 999.0, "j").cost_usd == 0.0


class TestMeteringEngine:
    def test_record_accumulates_per_tenant_buckets(self):
        m = MeteringEngine()
        m.record("gpu_usage", tenant="t1", gpu_seconds=120.0, job_id="j1")
        m.record("cpu_usage", tenant="t1", cpu_seconds=3600.0, job_id="j1")
        m.record("storage_usage", tenant="t1", gb_seconds=86400.0, job_id="j1")
        m.record("job_completed", tenant="t1", job_id="j1")
        m.record("plugin_exec", tenant="t1", plugin_count=5, job_id="j1")

        totals = m.snapshot("t1")
        assert totals["gpu_s"] == 120.0
        assert totals["cpu_s"] == 3600.0
        assert totals["gb_s"] == 86400.0
        assert totals["jobs"] == 1
        expected_cost = (
            120.0 * GPU_RATE + 3600.0 * CPU_RATE + 86400.0 * RAM_RATE + 5 * 0.001
        )
        assert totals["cost"] == pytest.approx(expected_cost)

    def test_event_value_takes_first_non_zero_dimension(self):
        m = MeteringEngine()
        ev = m.record("gpu_usage", tenant="t1", gpu_seconds=10.0, cpu_seconds=99.0)
        assert ev.value == 10.0
        ev2 = m.record("cpu_usage", tenant="t1", gpu_seconds=0, cpu_seconds=42.0)
        assert ev2.value == 42.0

    def test_snapshot_of_unknown_tenant_is_empty_and_global_view_counts_events(self):
        m = MeteringEngine()
        assert m.snapshot("nobody") == {}
        empty = m.snapshot()
        assert empty == {"tenants": {}, "total_events": 0, "total_cost": 0}

        m.record("gpu_usage", tenant="t1", gpu_seconds=100.0)
        m.record("gpu_usage", tenant="t2", gpu_seconds=200.0)
        global_view = m.snapshot()
        assert global_view["total_events"] == 2
        assert global_view["total_cost"] == pytest.approx(300.0 * GPU_RATE)
        assert set(global_view["tenants"]) == {"t1", "t2"}


class TestInvoicingEngine:
    def test_generate_totals_line_items_with_tax(self):
        engine = InvoicingEngine(tax_rate=0.2)
        items = [
            {"desc": "GPU compute", "cost": 0.05},
            {"desc": "Plugin exec", "cost": 0.01},
        ]
        inv = engine.generate("tenant-abc", items, period_start=100.0, period_end=200.0)

        assert inv.invoice_id.startswith("INV-")
        assert inv.tenant_id == "tenant-abc"
        assert inv.line_items == items
        assert inv.subtotal == pytest.approx(0.06)
        assert inv.tax == pytest.approx(0.012)
        assert inv.total == pytest.approx(0.072)
        assert inv.status == "draft"
        assert engine.invoices[inv.invoice_id] is inv

    def test_zero_tax_tier_and_issue_marks_invoice_issued(self):
        engine = InvoicingEngine(tax_rate=0.0)
        inv = engine.generate(
            "tenant-xyz", [{"desc": "free tier", "cost": 0.0}], 0.0, 1.0
        )
        assert inv.tax == 0.0
        assert inv.total == 0.0

        issued = engine.issue(inv.invoice_id)
        assert issued.status == "issued"
        assert isinstance(issued.issued_at, float)
        assert issued.issued_at <= time.time()


class TestUsageAggregator:
    @staticmethod
    def _store_with(events):
        store = BillingEventStore()
        for ev in events:
            store.append(ev)
        return store

    def test_aggregate_respects_time_window_and_tenant(self):
        now = 1_000_000.0
        store = self._store_with(
            [
                BillingEvent(
                    "e1",
                    "t1",
                    BillingEventType.GPU_ALLOCATED,
                    now - 100,
                    gpu_seconds=60.0,
                ),
                BillingEvent(
                    "e2", "t1", BillingEventType.JOB_STARTED, now - 10, gpu_seconds=30.0
                ),
                BillingEvent(
                    "e3",
                    "t2",
                    BillingEventType.GPU_ALLOCATED,
                    now - 10,
                    gpu_seconds=999.0,
                ),
            ]
        )
        agg = UsageAggregator(store)
        assert agg.aggregate_gpu_seconds("t1", now - 50, now) == pytest.approx(30.0)
        assert agg.aggregate_gpu_seconds("t1", now - 200, now) == pytest.approx(90.0)
        assert agg.aggregate_gpu_seconds("t3", now - 200, now) == 0.0

    def test_aggregate_by_type_groups_on_event_type_value(self):
        now = 2_000_000.0
        store = self._store_with(
            [
                BillingEvent(
                    "e1", "t1", BillingEventType.GPU_ALLOCATED, now, gpu_seconds=10.0
                ),
                BillingEvent(
                    "e2", "t1", BillingEventType.GPU_ALLOCATED, now, gpu_seconds=5.0
                ),
                BillingEvent(
                    "e3", "t1", BillingEventType.JOB_STARTED, now, gpu_seconds=1.0
                ),
            ]
        )
        by_type = UsageAggregator(store).aggregate_by_type("t1", now - 1, now)
        assert by_type == {
            "gpu.allocated": pytest.approx(15.0),
            "job.started": pytest.approx(1.0),
        }

    def test_monthly_summary_window_covers_the_month_of_the_timestamp(self):
        store = BillingEventStore()
        agg = UsageAggregator(store)
        mid_month = time.mktime((2026, 5, 15, 12, 0, 0, 0, 0, -1))
        store.append(
            BillingEvent(
                "e1", "t1", BillingEventType.GPU_ALLOCATED, mid_month, gpu_seconds=42.0
            )
        )

        summary = agg.monthly_summary("t1", mid_month)
        assert summary["gpu_seconds"] == pytest.approx(42.0)
        assert summary["period_start"] <= mid_month <= summary["period_end"]
        assert summary["by_type"] == {"gpu.allocated": pytest.approx(42.0)}

    def test_december_window_rolls_into_next_year(self):
        store = BillingEventStore()
        agg = UsageAggregator(store)
        december = time.mktime((2026, 12, 20, 12, 0, 0, 0, 0, -1))
        store.append(
            BillingEvent(
                "e1", "t1", BillingEventType.GPU_ALLOCATED, december, gpu_seconds=7.0
            )
        )

        summary = agg.monthly_summary("t1", december)
        assert summary["gpu_seconds"] == pytest.approx(7.0)
        end_year = time.localtime(summary["period_end"]).tm_year
        assert end_year == 2027


class TestBillingEventStore:
    def test_store_indexes_by_tenant_and_returns_last_event(self):
        store = BillingEventStore()
        assert store.get_for_tenant("t1") == []
        assert store.last_event("t1") is None

        first = BillingEvent("e1", "t1", BillingEventType.JOB_SUBMITTED, 100.0)
        second = BillingEvent("e2", "t1", BillingEventType.JOB_COMPLETED, 200.0)
        store.append(first)
        store.append(BillingEvent("e3", "t2", BillingEventType.JOB_SUBMITTED, 150.0))
        store.append(second)

        assert store.get_for_tenant("t1") == [first, second]
        assert store.last_event("t1") is second
        assert [e.event_id for e in store.get_in_range("t1", 100.0, 200.0)] == [
            "e1",
            "e2",
        ]
        assert [e.event_id for e in store.get_in_range("t1", 101.0, 199.0)] == []

    def test_event_maps_to_stripe_usage_record(self):
        ev = BillingEvent(
            "e1", "tenant-abc", BillingEventType.GPU_ALLOCATED, 123.5, gpu_seconds=60.0
        )
        assert ev.to_stripe_record() == {
            "name": "gpu.allocated",
            "value": 60.0,
            "timestamp": 123.5,
            "tenant_id": "tenant-abc",
        }


def test_simulated_usage_demo_emits_ten_events(capsys):
    """Демо-функция модуля агрегатора должна оставаться исполняемой: она показывает формат окна."""
    store = BillingEventStore()
    agg = UsageAggregator(store)
    simulate_usage(agg)
    out = capsys.readouterr().out
    assert "Aggregated GPU-seconds" in out
    assert "Monthly summary" in out
    assert len(store.get_for_tenant("tenant-abc")) == 10
