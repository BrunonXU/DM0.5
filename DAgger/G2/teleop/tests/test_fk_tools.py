from __future__ import annotations

import numpy as np

from g2teleop.model_audit import missing_names


def test_fk_math_dependency_smoke() -> None:
    rotation = np.eye(3)
    assert np.trace(rotation) == 3.0
    assert missing_names(["left", "right"], ["left", "right"]) == []
