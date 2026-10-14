"""Compatibility import; implementation lives in dagger_core."""
import sys
from dagger_core import gamepad_windows as _implementation
sys.modules[__name__] = _implementation
