"""OTLP-трейсинг: включается только по OTEL_EXPORTER_OTLP_ENDPOINT, без него старт не меняется."""

import sys

import main


def test_no_endpoint_disables_tracing(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("JAEGER_ENABLED", raising=False)
    monkeypatch.setattr(main, "tracer", None)

    assert main._setup_tracing() is False
    assert main.tracer is None
    assert main.app is not None


def test_endpoint_wires_otlp_exporter_without_network(monkeypatch):
    import opentelemetry.exporter.otlp.proto.grpc.trace_exporter as grpc_exporter
    import opentelemetry.sdk.trace.export as sdk_export

    created = {}

    class FakeOTLPSpanExporter:
        def __init__(self, **kwargs):
            created.update(kwargs)

    class FakeBatchSpanProcessor:
        def __init__(self, exporter):
            created["processor_exporter"] = exporter

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=0):
            return True

        def on_start(self, span, parent_context=None):
            pass

        def on_end(self, span):
            pass

    monkeypatch.setattr(grpc_exporter, "OTLPSpanExporter", FakeOTLPSpanExporter)
    monkeypatch.setattr(sdk_export, "BatchSpanProcessor", FakeBatchSpanProcessor)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    monkeypatch.delenv("JAEGER_ENABLED", raising=False)

    assert main._setup_tracing() is True
    assert created["endpoint"] == "http://127.0.0.1:4317"
    assert isinstance(created["processor_exporter"], FakeOTLPSpanExporter)


def test_jaeger_enabled_warns_and_does_not_crash(monkeypatch):
    warnings = []
    monkeypatch.setattr(main.logger, "warning", lambda msg, *a, **k: warnings.append(msg))
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("JAEGER_ENABLED", "true")

    assert main._setup_tracing() is False
    assert any("deprecated" in w for w in warnings)
    assert main.app is not None


def test_missing_exporter_disables_tracing(monkeypatch):
    warnings = []
    monkeypatch.setattr(main.logger, "warning", lambda msg, *a, **k: warnings.append(msg))
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
    monkeypatch.delenv("JAEGER_ENABLED", raising=False)
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.grpc.trace_exporter", None)

    assert main._setup_tracing() is False
    assert any("OTEL tracing disabled" in w for w in warnings)
