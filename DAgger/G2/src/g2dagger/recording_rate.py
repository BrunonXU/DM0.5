"""Read-only acquisition cadence diagnostic; never certifies training readiness."""
import math
import numpy as np
from .recording_observation import CAMERAS


def audit_capture_rate(rows, target_hz=30., tolerance=.05):
    if not math.isfinite(target_hz) or target_hz <= 0 or not 0 <= tolerance < 1:
        raise ValueError("invalid target/tolerance")
    errors = []
    def cadence(times):
        intervals = np.diff(times) / 1e9
        valid = len(intervals) > 0 and bool(np.all(intervals > 0))
        return dict(count=len(times), host_time_increasing=valid,
                    hz=float(1 / np.mean(intervals)) if valid else None,
                    max_gap_ms=float(max(intervals)*1000) if valid else None)
    try:
        observations = [row['acquisition'] for row in rows]
        result = cadence([o['capture_started_monotonic_ns'] for o in observations])
        cameras = {}
        for camera in CAMERAS:
            frames = []
            for observation in observations:
                matches = [f for f in observation['frames'] if f['camera'] == camera]
                if len(matches) != 1:
                    raise ValueError('missing/duplicate camera')
                frames.append(matches[0])
            fresh, previous, repeats, regressions = [], None, 0, 0
            domains = set()
            for frame in frames:
                stamp = frame['timestamp']
                if type(stamp['value']) is not int or not stamp['unit'] or not stamp['clock_domain']:
                    raise ValueError('invalid device timestamp')
                domains.add((stamp['unit'], stamp['clock_domain']))
                identity = (frame['frame_id'], stamp['value'])
                if previous is None or (identity[0] > previous[0] and identity[1] > previous[1]):
                    fresh.append(frame['host_received_monotonic_ns'])
                elif identity == previous:
                    repeats += 1
                else:
                    regressions += 1
                previous = identity
            report = cadence(fresh)
            report.update(repeated_frames=repeats, inconsistent_or_regressed_frames=regressions,
                          timestamp_domains=[list(d) for d in sorted(domains)])
            cameras[camera] = report
            if len(domains) != 1 or regressions:
                errors.append(f'{camera}: inconsistent timestamps/frame IDs')
            if report['hz'] is None or report['hz'] < target_hz*(1-tolerance):
                errors.append(f'{camera}: fresh frame rate below target')
        if result['hz'] is None or result['hz'] < target_hz*(1-tolerance):
            errors.append('observation rate below target')
        return dict(target_hz=target_hz, tolerance=tolerance, observation=result, cameras=cameras,
                    target_rate_met=not errors, training_ready=False, errors=errors,
                    scope='Host arrival cadence only; no clock synchronization, payload or 34D certification')
    except (KeyError, TypeError, ValueError) as exc:
        return dict(target_rate_met=False, training_ready=False, errors=[f'malformed acquisition: {exc}'])
