"""Compatibility import; implementation lives in dagger_core."""
import sys
from dagger_core import hil_shadow_server as _implementation
sys.modules[__name__] = _implementation
