#!/usr/bin/env python3
"""Compatibility redirect: implementation has moved to sibling DAgger."""
from pathlib import Path
import runpy
import sys
root = Path(__file__).resolve().parents[2] / "DAgger"
sys.path.insert(0, str(root / "src"))
print("MOVED: use " + str(root / "START_HERE.md"), file=sys.stderr)
runpy.run_path(str(root / "scripts" / Path(__file__).name), run_name="__main__")
