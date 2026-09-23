from g2dagger.recording_rate import audit_capture_rate
from g2dagger.recording_observation import CAMERAS


def rows(hz, repeat=1):
    return [{'acquisition': {'capture_started_monotonic_ns': round(i*1e9/hz),
        'frames': [{'camera': camera, 'frame_id': i//repeat,
                    'timestamp': {'value': round((i//repeat)*repeat*1e9/hz), 'unit': 'ns', 'clock_domain': 'device'},
                    'host_received_monotonic_ns': round(i*1e9/hz)} for camera in CAMERAS]}}
        for i in range(90)]


def test_thirty_fresh_frames_pass_rate_only():
    result = audit_capture_rate(rows(30))
    assert result['target_rate_met'] and not result['training_ready']


def test_ten_hz_or_repeated_frames_cannot_be_relabelled_thirty():
    assert not audit_capture_rate(rows(10))['target_rate_met']
    result = audit_capture_rate(rows(30, 3))
    assert not result['target_rate_met']
    assert result['cameras']['head']['repeated_frames'] == 60
    assert abs(result['cameras']['head']['hz'] - 10) < .001


def test_empty_missing_camera_or_clock_change_fail_closed():
    assert not audit_capture_rate([])['target_rate_met']
    records = rows(30)
    records[1]['acquisition']['frames'][0]['timestamp']['clock_domain'] = 'other'
    assert not audit_capture_rate(records)['target_rate_met']
    records[1]['acquisition']['frames'] = []
    assert not audit_capture_rate(records)['target_rate_met']
