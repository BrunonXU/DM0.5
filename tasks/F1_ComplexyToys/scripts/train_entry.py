#!/usr/bin/env python3
"""Run the upstream SFT entrypoint and close only its private Ray cluster."""
import os
import runpy
from pathlib import Path

import ray

if __name__ == "__main__":
    if os.environ.get("RAY_ADDRESS") != "local":
        raise RuntimeError("This smoke launcher requires its own local Ray instance")
    try:
        runpy.run_path(str(Path(os.environ["RLINF_ROOT"]) / "examples/sft/train_vla_sft.py"), run_name="__main__")
    finally:
        ray.shutdown()
