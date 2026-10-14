"""Compatibility import; implementation lives in dagger_core."""
import sys
from dagger_core import async_transport as _implementation
sys.modules[__name__] = _implementation
