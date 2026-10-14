"""Read-only Pinocchio model audit helpers; no GDK or robot commands."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def missing_names(observed: Iterable[str], expected: Iterable[str]) -> list[str]:
    observed_set = set(observed)
    return [name for name in expected if name not in observed_set]


def build_reduced_arm_model(
    pin: Any,
    full_model: Any,
    side_joints: Iterable[str],
    reference_q: Any | None = None,
) -> Any:
    """Lock every joint except one named seven-joint arm.

    ``reference_q`` is required for runtime/replay use so the fixed base and
    waist placement matches the measured takeover state.  Neutral remains the
    default only for the structure audit.
    """

    keep = set(side_joints)
    absent = missing_names(full_model.names, keep)
    if absent:
        raise ValueError(f"arm joints missing from model: {absent}")
    lock_configuration = pin.neutral(full_model) if reference_q is None else reference_q
    lock_ids = [
        joint_id
        for joint_id, name in enumerate(full_model.names)
        if joint_id > 0 and name not in keep
    ]
    reduced = pin.buildReducedModel(full_model, lock_ids, lock_configuration)
    if reduced.nq != len(keep) or reduced.nv != len(keep):
        raise ValueError(
            f"unexpected reduced dimensions nq={reduced.nq} nv={reduced.nv}"
        )
    return reduced
