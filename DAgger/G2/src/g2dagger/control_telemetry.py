"""Compatibility import; implementation lives in dagger_core."""
import sys
from dagger_core import control_telemetry as _implementation
sys.modules[__name__] = _implementation
