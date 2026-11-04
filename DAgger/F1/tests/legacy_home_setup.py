"""Explicit frozen clearance configuration for backwards-compatibility tests."""
import copy
import pytest
from test_f1_numerical import setup as numerical_setup


@pytest.fixture(scope='module')
def setup(numerical_setup):
    c, p, m, u, full = numerical_setup
    p = copy.deepcopy(p)
    p['f710_home'].pop('route', None)
    p['f710_home']['clearance'] = dict(schema='f1_right_clearance_v2',
        frame='base_link', lift_m=.06, right_m=.10, max_rotation_deg=10.)
    return c, p, m, u, full
