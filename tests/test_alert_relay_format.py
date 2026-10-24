"""P1-T5 alert relay: формат входящего webhook — Alertmanager v4 и legacy Grafana.

Живой фикс (artifacts/alert-relay) отличался от канона ровно одним блоком в
`format_payload`: Alertmanager v4 присылает объект, а legacy Grafana webhook —
голый список. До фикса `payload.get("title")` на списке падал AttributeError,
и релей отвечал 400/500 вместо доставки.

Тест грузит КАНОНИЧЕСКИЙ файл `deploy/ops/alert_relay.py` (путь из репозитория),
а не исполняемую копию: доказательство должно относиться к версионируемому коду.
Сеть не затрагивается — `send_telegram` не вызывается, проверяется только текст.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

pytestmark = pytest.mark.ops

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CANON = REPO_ROOT / "deploy" / "ops" / "alert_relay.py"


def _load_relay():
    spec = importlib.util.spec_from_file_location("alert_relay_under_test", CANON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_alertmanager_v4_title_from_common_labels() -> None:
    """v4: заголовок берётся из commonLabels.alertname, если title не задан."""
    relay = _load_relay()
    payload = {
        "version": "4",
        "status": "firing",
        "commonLabels": {"alertname": "ZoDeliveryProbe", "severity": "warning"},
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "ZoDeliveryProbe", "severity": "warning"},
                "annotations": {"summary": "проверка канала доставки"},
            }
        ],
    }

    text = relay.format_payload(payload)

    lines = text.splitlines()
    assert lines[0] == "[FIRING] ZoDeliveryProbe"
    assert lines[1] == "- firing: ZoDeliveryProbe | проверка канала доставки"


def test_legacy_grafana_list_payload_rendered() -> None:
    """legacy Grafana: голый список алертов не роняет формат и доезжает целиком."""
    relay = _load_relay()
    payload = [
        {
            "status": "alerting",
            "labels": {"alertname": "WALGBackupFailed"},
            "annotations": {"description": "last backup failed"},
        }
    ]

    text = relay.format_payload(payload)

    lines = text.splitlines()
    assert lines[0] == "[?] Grafana alert"
    assert lines[1] == "- alerting: WALGBackupFailed | last backup failed"


def test_explicit_title_wins_over_common_labels() -> None:
    """Обратная совместимость: явный title (старый формат релея) имеет приоритет."""
    relay = _load_relay()
    payload = {
        "title": "AstroFin alert",
        "status": "resolved",
        "commonLabels": {"alertname": "SomethingElse"},
        "alerts": [],
    }

    text = relay.format_payload(payload)

    assert text.splitlines()[0] == "[RESOLVED] AstroFin alert"
