"""Read bounded, same-process owner startup diagnostics without inferring readiness."""
import json
import math
from pathlib import Path


STAGES = ('runtime_contract', 'ros_transport', 'first_valid_feedback',
          'collision_gate', 'arm_model', 'owner_initialize', 'ik_warmup', 'harness_bridge')
MAX_LOG_BYTES = 1024 * 1024
PREFIX = b'F1 STARTUP '


def _extra_timing(raw, prefix, schema, fields):
    rows = [line[len(prefix):] for line in raw.splitlines() if line.startswith(prefix)]
    if len(rows) != 1:
        return None
    try:
        row = json.loads(rows[0])
        if row.get('schema') != schema:
            return None
        if any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) or row[k] < 0 for k in fields):
            return None
        return {k: row[k] for k in fields}
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        return None


def owner_startup_report(run):
    result = dict(source='owner.log', status='unavailable', stages=[],
                  expected_stage_count=len(STAGES), invalid_records=0,
                  scan_truncated=False, measured_stage_s=None, observed_span_s=None)
    result.update(native_entry=None, static_model=None)
    try:
        with (Path(run) / 'owner.log').open('rb') as stream:
            raw = stream.read(MAX_LOG_BYTES + 1)
    except FileNotFoundError:
        return result
    except OSError:
        result['status'] = 'unreadable'
        return result
    result['scan_truncated'] = len(raw) > MAX_LOG_BYTES
    if result['scan_truncated']:
        raw = raw[:MAX_LOG_BYTES].rsplit(b'\n', 1)[0] if b'\n' in raw[:MAX_LOG_BYTES] else b''
    result['native_entry'] = _extra_timing(raw, b'F1 NATIVE STARTUP ', 'f1-native-startup-v1',
        ('source_integrity_s', 'native_import_s', 'entry_to_owner_main_s'))
    result['static_model'] = _extra_timing(raw, b'F1 MODEL STARTUP ', 'f1-model-startup-v1',
        ('static_prepare_s', 'remaining_wait_s', 'feedback_bind_s', 'overlapped_prepare_s'))
    stages = result['stages']
    for line in raw.splitlines():
        if not line.startswith(PREFIX):
            continue
        try:
            row = json.loads(line[len(PREFIX):])
            if (not isinstance(row, dict) or row.get('schema') != 'f1-owner-startup-timing-v1'
                    or type(row.get('complete')) is not bool):
                raise ValueError('invalid_record')
            elapsed, end = row.get('elapsed_s'), row.get('since_start_s')
            if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
                   for v in (elapsed, end)):
                raise ValueError('invalid_interval')
            previous = stages[-1]['since_start_s'] if stages else 0
            if (len(stages) >= len(STAGES) or row.get('stage') != STAGES[len(stages)]
                    or (stages and not stages[-1]['complete'])
                    or end - elapsed < previous - 1e-6):
                raise ValueError('ambiguous_sequence')
            stages.append({key: row[key] for key in ('stage', 'elapsed_s', 'since_start_s', 'complete')})
        except (ValueError, TypeError, OverflowError, RecursionError):
            result['invalid_records'] += 1
    if result['invalid_records']:
        result['status'] = 'invalid'
    elif stages:
        result.update(status='complete' if len(stages) == len(STAGES) and stages[-1]['complete'] else 'partial',
                      measured_stage_s=sum(row['elapsed_s'] for row in stages),
                      observed_span_s=stages[-1]['since_start_s'])
    return result
