"""Speed-adaptive input conditioning, before IK; metres, radians, wxyz.

Inspired by speed-adaptive low-pass filtering (1 Euro). This implementation
also integrates continuously scaled increments; it is not the reference
1 Euro algorithm, and does not claim physical positioning accuracy.
"""
from __future__ import annotations
import math
import numpy as np


DEFAULTS = dict(enabled=True, position_min_gain=.2, rotation_min_gain=.25,
    position_slow_m_s=.005, position_fast_m_s=.04,
    rotation_slow_rad_s=.04, rotation_fast_rad_s=.35,
    filter_position_slow_m_s=.025, filter_position_fast_m_s=.15,
    filter_rotation_slow_rad_s=.15, filter_rotation_fast_rad_s=1.,
    min_cutoff_hz=3.5, max_cutoff_hz=15., derivative_cutoff_hz=2.,
    gain_cutoff_hz=5.)


class PrecisionInputError(ValueError):
    """Invalid pose input; the affected hand stays disabled until Grip release."""


def precision_settings(value=None):
    if value is None:
        value = {'enabled': False}  # Legacy configs keep their old mapping.
    if not isinstance(value, dict) or set(value)-set(DEFAULTS):
        raise ValueError('precision must be an object with known fields')
    cfg = {**DEFAULTS, **value}
    if type(cfg['enabled']) is not bool:
        raise ValueError('precision.enabled must be boolean')
    for key in set(DEFAULTS)-{'enabled'}:
        raw = cfg[key]
        if isinstance(raw, bool) or not isinstance(raw, (float, int)) or not math.isfinite(raw) or raw <= 0:
            raise ValueError(f'precision.{key} must be a positive finite number')
    for key in ('position_min_gain', 'rotation_min_gain'):
        if not .05 <= cfg[key] <= 1.:
            raise ValueError(f'precision.{key} must be within [.05, 1]')
    for lo, hi in (('position_slow_m_s', 'position_fast_m_s'),
                   ('rotation_slow_rad_s', 'rotation_fast_rad_s'),
                   ('filter_position_slow_m_s', 'filter_position_fast_m_s'),
                   ('filter_rotation_slow_rad_s', 'filter_rotation_fast_rad_s'),
                   ('min_cutoff_hz', 'max_cutoff_hz')):
        if cfg[lo] >= cfg[hi]:
            raise ValueError(f'precision.{lo} must be below {hi}')
    if cfg['max_cutoff_hz'] > 25:
        raise ValueError('precision.max_cutoff_hz must be <=25')
    return cfg


def _unit(q):
    q = np.asarray(q, dtype=float)
    if q.shape != (4,) or not np.all(np.isfinite(q)) or np.linalg.norm(q) < 1e-9:
        raise ValueError('finite nonzero wxyz quaternion required')
    return q/np.linalg.norm(q)


def _mul(a, b):
    return np.r_[a[0]*b[0]-np.dot(a[1:],b[1:]),
                 a[0]*b[1:]+b[0]*a[1:]+np.cross(a[1:],b[1:])]


def _delta(a, b):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    q = _unit(_mul(a, np.r_[b[0], -b[1:]]))
    if q[0] < 0: q = -q  # shortest arc; q and -q are the same attitude
    n = np.linalg.norm(q[1:])
    return 2*q[1:] if n < 1e-10 else q[1:]*(2*math.atan2(n,q[0])/n)


def _exp(v):
    n = np.linalg.norm(v)
    return _unit(np.r_[math.cos(n/2), v*(.5 if n < 1e-10 else math.sin(n/2)/n)])


def _alpha(cutoff, dt):
    return -math.expm1(-2*math.pi*cutoff*dt)


def _blend(speed, lo, hi):
    x = float(np.clip((speed-lo)/(hi-lo), 0., 1.))
    return x*x*(3-2*x)


class PrecisionPoseMapper:
    def __init__(self, settings=None):
        self.cfg = precision_settings(DEFAULTS if settings is None else settings)
        self.reset()

    def reset(self):
        self.t = None

    def update(self, position, quaternion, now):
        p = np.asarray(position, dtype=float)
        q = _unit(quaternion)
        if p.shape != (3,) or not np.all(np.isfinite(p)) or not math.isfinite(now):
            raise ValueError('finite position and timestamp required')
        c = self.cfg
        if not c['enabled']:
            return p.copy(), q.copy()
        if self.t is None:
            self.raw_p = self.filtered_p = self.output_p = p.copy()
            self.raw_q = self.filtered_q = self.output_q = q.copy()
            self.velocity = np.zeros(3); self.omega = np.zeros(3)
            self.position_gain = c['position_min_gain']
            self.rotation_gain = c['rotation_min_gain']
            self.t = now
            return p.copy(), q.copy()
        dt = now-self.t
        if dt < 0 or dt > .15:
            raise ValueError('precision input clock stale; release Grip to re-anchor')
        if dt == 0:
            return self.output_p.copy(), self.output_q.copy()
        dp = p-self.raw_p; dr = _delta(q,self.raw_q)
        if np.linalg.norm(dp) > .25 or np.linalg.norm(dr) > 1.2:
            raise ValueError('precision tracking jump; release Grip to re-anchor')
        d_alpha = _alpha(c['derivative_cutoff_hz'],dt)
        self.velocity += d_alpha*(dp/dt-self.velocity)
        self.omega += d_alpha*(dr/dt-self.omega)
        bp = _blend(np.linalg.norm(self.velocity), c['position_slow_m_s'],c['position_fast_m_s'])
        br = _blend(np.linalg.norm(self.omega), c['rotation_slow_rad_s'],c['rotation_fast_rad_s'])
        a_gain = _alpha(c['gain_cutoff_hz'],dt)
        self.position_gain += a_gain*(c['position_min_gain']+(1-c['position_min_gain'])*bp-self.position_gain)
        self.rotation_gain += a_gain*(c['rotation_min_gain']+(1-c['rotation_min_gain'])*br-self.rotation_gain)
        # Gain intent and noise bandwidth are separate. Lowering the precision
        # speed thresholds must not also open the filter to hand tremor.
        fp = _blend(np.linalg.norm(self.velocity), c['filter_position_slow_m_s'],c['filter_position_fast_m_s'])
        fr = _blend(np.linalg.norm(self.omega), c['filter_rotation_slow_rad_s'],c['filter_rotation_fast_rad_s'])
        cp = c['min_cutoff_hz']+fp*(c['max_cutoff_hz']-c['min_cutoff_hz'])
        cr = c['min_cutoff_hz']+fr*(c['max_cutoff_hz']-c['min_cutoff_hz'])
        next_p = self.filtered_p+_alpha(cp,dt)*(p-self.filtered_p)
        next_q = _unit(_mul(_exp(_alpha(cr,dt)*_delta(q,self.filtered_q)),self.filtered_q))
        # Scale increments, never an absolute offset: a changing gain cannot
        # jump back toward the clutch origin or amplify the accumulated offset.
        self.output_p = self.output_p+self.position_gain*(next_p-self.filtered_p)
        self.output_q = _unit(_mul(_exp(self.rotation_gain*_delta(next_q,self.filtered_q)),self.output_q))
        self.raw_p,self.raw_q = p.copy(),q.copy()
        self.filtered_p,self.filtered_q = next_p,next_q
        self.t = now
        return self.output_p.copy(),self.output_q.copy()
