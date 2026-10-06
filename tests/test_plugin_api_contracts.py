"""N-BLACK-B: поведенческие тесты контракта плагинов и рантайма плагинов.

Зачем: plugins/plugin_api.py и plugins/plugin_runtime.py не покрывались.
Проверяется: неизменяемая подпись задачи (fingerprint от payload), валидация обязательных полей,
исполнение плагина при достаточном и недостаточном GPU, реестр плагинов и отказ по неизвестному имени,
порядок версий, реестр хуков и загрузка экземпляра плагина из класса.
"""

import asyncio
import dataclasses

import pytest

from plugins.plugin_api import (
    PLUGIN_REGISTRY,
    ETLPipelinePlugin,
    InferencePlugin,
    MLTrainingPlugin,
    PluginCapability,
    PluginPriority,
    PluginResult,
    ROMATask,
    SimulationPlugin,
    ValidationResult,
    get_plugin,
)
from plugins.plugin_runtime import (
    IsolationLevel,
    PluginInstance,
    PluginRuntime,
    PluginVersion,
)


@dataclasses.dataclass
class FakeContext:
    gpu_available: bool = True
    vram_gb: float = 24.0
    cpu_cores: int = 16
    ram_gb: int = 64
    node_name: str = "gpu-node-1"
    tick: int = 7


def _task(
    payload: dict, task_id: str = "task-1", plugin: str = "ml_training"
) -> ROMATask:
    return ROMATask(
        task_id=task_id, plugin_name=plugin, payload=payload, metadata={"tenant": "t-1"}
    )


def test_task_fingerprint_is_stable_for_equal_payload():
    first = _task({"epochs": 10, "model_type": "yolo"})
    reordered = _task({"model_type": "yolo", "epochs": 10})
    different = _task({"epochs": 11, "model_type": "yolo"})

    assert first.fingerprint == reordered.fingerprint
    assert first.fingerprint != different.fingerprint
    assert len(first.fingerprint) == 16


def test_plugin_result_to_dict_reports_timestamp_and_defaults():
    result = PluginResult(success=False, error="boom")

    payload = result.to_dict()

    assert payload["success"] is False
    assert payload["error"] == "boom"
    assert payload["output"] is None
    assert payload["metrics"] == {}
    assert payload["timestamp"] <= result._timestamp + 1e-6
    assert ValidationResult(valid=True).errors == []
    assert ValidationResult(valid=False, errors=["model_type"]).errors == ["model_type"]


def test_registry_exposes_builtin_plugins_with_declared_capabilities():
    assert set(PLUGIN_REGISTRY) == {
        "ml_training",
        "inference",
        "etl_pipeline",
        "simulation",
    }

    ml = get_plugin("ml_training")
    assert isinstance(ml, MLTrainingPlugin)
    assert ml.name == "ml_training"
    assert ml.version == "1.0.0"
    assert ml.priority is PluginPriority.HIGH
    assert ml.capabilities == [
        PluginCapability.GPU_ENABLED,
        PluginCapability.DISTRIBUTED,
    ]

    inference = get_plugin("inference")
    assert inference.priority is PluginPriority.CRITICAL
    assert ETLPipelinePlugin().capabilities == [
        PluginCapability.STATEFUL,
        PluginCapability.PERSISTENT_STORAGE,
    ]
    assert SimulationPlugin().priority is PluginPriority.LOW

    with pytest.raises(ValueError):
        get_plugin("not_registered")


def test_ml_plugin_validation_requires_mandatory_payload_fields():
    plugin = MLTrainingPlugin()

    invalid = asyncio.run(plugin.on_validate(_task({"epochs": 3})))
    valid = asyncio.run(
        plugin.on_validate(
            _task({"model_type": "yolo", "dataset": "coco", "batch_size": 8})
        )
    )

    assert invalid.valid is False
    assert invalid.errors == ["model_type", "dataset", "batch_size"]
    assert valid.valid is True
    assert valid.errors == []


def test_ml_plugin_executes_only_with_enough_vram():
    plugin = MLTrainingPlugin()
    task = _task({"batch_size": 8, "epochs": 4})
    asyncio.run(plugin.on_init({"region": "eu-west"}))
    assert plugin.config == {"region": "eu-west"}

    ok = asyncio.run(plugin.on_execute(task, FakeContext(vram_gb=24.0)))
    assert ok.success is True
    assert ok.output["epochs_completed"] == 4
    assert ok.output["gpu_used"] == "gpu-node-1"
    assert ok.metrics["duration_s"] == pytest.approx(4 * 2.5)
    assert ok.metrics["vram_gb"] == pytest.approx(8 * 0.7)

    too_small = asyncio.run(plugin.on_execute(task, FakeContext(vram_gb=2.0)))
    assert too_small.success is False
    assert too_small.error == "Insufficient GPU resources"

    no_gpu = asyncio.run(plugin.on_execute(task, FakeContext(gpu_available=False)))
    assert no_gpu.success is False


def test_reference_plugins_return_their_own_output_shapes():
    inference = InferencePlugin()
    etl = ETLPipelinePlugin()
    simulation = SimulationPlugin()

    inference_out = asyncio.run(
        inference.on_execute(
            _task({"model": "llama"}, plugin="inference"), FakeContext()
        )
    )
    assert inference_out.success is True
    assert inference_out.output == {
        "inference_id": "task-1",
        "model": "llama",
        "node": "gpu-node-1",
    }

    etl_out = asyncio.run(
        etl.on_execute(_task({}, plugin="etl_pipeline"), FakeContext())
    )
    assert etl_out.output == {"pipeline_id": "task-1", "stage": "completed"}

    sim_out = asyncio.run(
        simulation.on_execute(_task({}, plugin="simulation"), FakeContext())
    )
    assert sim_out.output == {"sim_id": "task-1"}

    assert asyncio.run(inference.on_cleanup()) is None


def test_plugin_version_string_and_ordering():
    older = PluginVersion(1, 2, 3)
    newer = PluginVersion(1, 3, 0)

    assert str(older) == "1.2.3"
    assert older < newer
    assert not (newer < older)


def test_runtime_loads_plugin_class_and_keeps_spec_metadata(tmp_path):
    runtime = PluginRuntime(plugin_dir=str(tmp_path))
    loaded: list[PluginInstance] = []
    runtime.register_hook("on_load", loaded.append)

    instance = runtime.load_from_class(MLTrainingPlugin, "ml_training", version="2.0.1")

    assert isinstance(instance, PluginInstance)
    assert isinstance(instance.instance, MLTrainingPlugin)
    assert instance.spec.name == "ml_training"
    assert str(instance.spec.version) == "2.0.1"
    assert instance.spec.entry_point == "class:MLTrainingPlugin"
    assert instance.spec.isolation is IsolationLevel.PROCESS
    assert instance.spec.checksum == "builtin"
    # N-BLACK-B-2 (находка, не правится в этом шаге): load_from_class читает
    # metadata через getattr по КЛАССУ, поэтому в spec попадает объект-property,
    # а не значение. Для ЗАГРУЖЕННОГО экземпляра значение доступно как обычно.
    assert isinstance(instance.spec.priority, property)
    assert instance.instance.priority == PluginPriority.HIGH
    assert instance.phase == "initializing"
    assert loaded == [instance]
    assert runtime._loaded["ml_training"] is instance


def test_runtime_rejects_unknown_hook(tmp_path):
    runtime = PluginRuntime(plugin_dir=str(tmp_path))

    with pytest.raises(ValueError):
        runtime.register_hook("on_apocalypse", lambda _: None)
