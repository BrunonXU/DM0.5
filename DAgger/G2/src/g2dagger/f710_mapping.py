"""Compatibility import; implementation lives in dagger_core."""
import sys
from dagger_core import f710_mapping as _implementation
sys.modules[__name__] = _implementation
