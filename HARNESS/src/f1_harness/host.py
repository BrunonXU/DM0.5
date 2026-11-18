"""Explicit offline fixture and RPent host construction (no implicit real robot)."""
from pathlib import Path

from .adapters import F1Toolkit, ScopedBackend, ToolkitBackend
from .contracts import Mode, Task
from .engine import TaskEngine
from .evidence import EvidenceStore
from .fake import FakePlant


def fake_host(run: Path, *, failures=None):
    store = EvidenceStore(run)
    store.event("run_identity", scenario_id=run.name, synthetic=True)
    engine = TaskEngine(store, mode=Mode.FAKE, lease_ms=2000)
    session = engine.open_session()["session_id"]
    engine.prepare_task(session, Task(task_id=run.name, object_id="square-1", hole_id="square-hole-1",
                                     calibration_id="synthetic-calibration").model_dump())
    plant = FakePlant(engine, failures)
    plant.start()
    backend = ScopedBackend(engine, plant, session_id=session, plant=plant)
    toolkit = F1Toolkit(backend, run / "memory")
    return engine, plant, ToolkitBackend(toolkit), store
