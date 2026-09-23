#!/usr/bin/env python3
"""6000D CPU client for paired F1 inference endpoints; never sends robot commands."""
from pathlib import Path
import sys

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK/'models/DM0.5'))

from f1_inference.compare import main


if __name__ == '__main__':
    main()
