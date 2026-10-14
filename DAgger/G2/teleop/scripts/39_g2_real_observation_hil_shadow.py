#!/usr/bin/env python3
"""Compatibility redirect: implementation is owned by the G2 DAgger platform."""
from pathlib import Path
import runpy
import sys
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root.parent / "shared/src"))
sys.path.insert(0, str(root / "src"))
print("MOVED: use " + str(root.parent / "docs/G2/START_HERE.md"), file=sys.stderr)
runpy.run_path(str(root / "scripts" / Path(__file__).name), run_name="__main__")
