"""Read ARKit pose and controls from HEBI Mobile I/O without robot access."""

from __future__ import annotations

import math
import time
from typing import Any

from .contracts import IpadSample


class IpadConnectionError(RuntimeError):
    pass


class IpadHebiReader:
    """Discover and read one HEBI Mobile I/O device.

    Importing this module does not require ``hebi-py``. The dependency is
    loaded only when ``connect`` is called, keeping logic tests hardware-free.
    """

    def __init__(
        self,
        *,
        family: str = "HEBI",
        name: str = "mobileIO",
        discovery_seconds: float = 2.0,
        hebi_module: Any | None = None,
    ) -> None:
        if not family.strip() or not name.strip():
            raise ValueError("HEBI family and name must not be empty")
        if discovery_seconds < 0:
            raise ValueError("discovery_seconds must be non-negative")
        self.family = family
        self.name = name
        self.discovery_seconds = float(discovery_seconds)
        self._hebi = hebi_module
        self._group: Any | None = None

    @property
    def is_connected(self) -> bool:
        return self._group is not None

    def connect(self) -> None:
        if self.is_connected:
            return
        if self._hebi is None:
            try:
                import hebi as hebi_module
            except ImportError as exc:
                raise IpadConnectionError(
                    "hebi-py is required; install the G2Teleop ipad extra "
                    "on the non-G2 development computer"
                ) from exc
            self._hebi = hebi_module
        lookup = self._hebi.Lookup()
        if self.discovery_seconds:
            time.sleep(self.discovery_seconds)
        group = lookup.get_group_from_names([self.family], [self.name])
        if group is None:
            raise IpadConnectionError(
                f"HEBI device {self.family}/{self.name} was not discovered; "
                "keep Mobile I/O in foreground and check the local network"
            )
        self._group = group

    def disconnect(self) -> None:
        self._group = None

    def read(self) -> IpadSample:
        if self._group is None:
            raise IpadConnectionError("iPad reader is not connected")
        feedback = self._group.get_next_feedback()
        if feedback is None or len(feedback) < 1:
            raise IpadConnectionError("HEBI returned no feedback")
        pose = feedback[0]
        raw_position = getattr(pose, "ar_position", None)
        raw_quaternion = getattr(pose, "ar_orientation", None)
        if raw_position is None or raw_quaternion is None:
            raise IpadConnectionError("HEBI feedback has no ARKit pose")
        position = tuple(float(value) for value in raw_position)
        quaternion = tuple(float(value) for value in raw_quaternion)
        if len(position) != 3 or len(quaternion) != 4:
            raise IpadConnectionError("HEBI returned an invalid ARKit pose shape")
        if not all(math.isfinite(value) for value in position + quaternion):
            raise IpadConnectionError("HEBI returned a non-finite ARKit pose")

        analogs: dict[str, float] = {}
        buttons: dict[str, bool] = {}
        io = getattr(pose, "io", None)
        bank_a = getattr(io, "a", None) if io is not None else None
        bank_b = getattr(io, "b", None) if io is not None else None
        if bank_a is not None:
            for index in range(1, 9):
                if bank_a.has_float(index):
                    analogs[f"a{index}"] = float(bank_a.get_float(index))
        if bank_b is not None:
            for index in range(1, 9):
                if bank_b.has_int(index):
                    buttons[f"b{index}"] = bool(bank_b.get_int(index))
                elif hasattr(bank_b, "has_bool") and bank_b.has_bool(index):
                    buttons[f"b{index}"] = bool(bank_b.get_bool(index))

        return IpadSample(
            captured_at_monotonic=time.monotonic(),
            position_m=position,
            quaternion_wxyz=quaternion,
            analogs=analogs,
            buttons=buttons,
        )

