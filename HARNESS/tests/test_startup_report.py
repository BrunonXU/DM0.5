import json
from pathlib import Path

import pytest

from f1_harness.startup_report import MAX_LOG_BYTES, STAGES, owner_startup_report
from f1_harness.timing_report import timing_report


def records():
    return [dict(schema='f1-owner-startup-timing-v1', stage=name,
                 elapsed_s=0.5, since_start_s=i + 1.0, complete=True)
            for i, name in enumerate(STAGES)]


def write_log(root, rows):
    (root / 'owner.log').write_text('unrelated ROS output\n' + '\n'.join(
        'F1 STARTUP ' + json.dumps(row) for row in rows) + '\n')


def test_full_report_keeps_unmeasured_gaps_and_task_outcome_separate(tmp_path):
    write_log(tmp_path, records())
    report = timing_report(tmp_path)
    startup = report['owner_startup']
    assert startup['status'] == 'complete'
    assert len(startup['stages']) == 8
    assert startup['measured_stage_s'] == 4
    assert startup['observed_span_s'] == 8
    assert report['click_to_first_publication_receipt_s'] is None
    assert report['full_grasp_place_reset_s'] is None


def test_missing_and_legacy_log_are_not_zero_duration(tmp_path):
    for content in (None, 'F1 owner initialized\n'):
        if content is not None:
            (tmp_path / 'owner.log').write_text(content)
        report = owner_startup_report(tmp_path)
        assert report['status'] == 'unavailable'
        assert report['measured_stage_s'] is None


def test_parallel_model_and_native_entry_are_separate_not_summed(tmp_path):
    write_log(tmp_path, records())
    native = dict(schema='f1-native-startup-v1', source_integrity_s=.1,
                  native_import_s=.2, entry_to_owner_main_s=.8)
    model = dict(schema='f1-model-startup-v1', static_prepare_s=2.,
                 remaining_wait_s=.6, feedback_bind_s=.01, overlapped_prepare_s=1.4)
    with (tmp_path/'owner.log').open('a') as stream:
        stream.write('F1 NATIVE STARTUP '+json.dumps(native)+'\n')
        stream.write('F1 MODEL STARTUP '+json.dumps(model)+'\n')
    report = owner_startup_report(tmp_path)
    assert report['measured_stage_s'] == 4.
    assert report['native_entry']['entry_to_owner_main_s'] == .8
    assert report['static_model']['overlapped_prepare_s'] == 1.4
    with (tmp_path/'owner.log').open('a') as stream:
        stream.write('F1 MODEL STARTUP '+json.dumps(model)+'\n')
    assert owner_startup_report(tmp_path)['static_model'] is None


@pytest.mark.parametrize('failed', [False, True])
def test_partial_startup_preserves_failed_stage(tmp_path, failed):
    rows = records()[:3]
    rows[-1]['complete'] = not failed
    write_log(tmp_path, rows)
    report = owner_startup_report(tmp_path)
    assert report['status'] == 'partial'
    assert report['stages'][-1]['complete'] is not failed
    assert report['measured_stage_s'] == 1.5


@pytest.mark.parametrize('bad', [None, [], {}, {'complete': 'true'},
    dict(records()[0], elapsed_s=-1), dict(records()[0], elapsed_s=True),
    dict(records()[0], elapsed_s=float('nan')), dict(records()[0], since_start_s=float('inf')),
    dict(records()[0], elapsed_s=2), dict(records()[0], stage='unknown')])
def test_invalid_diagnostics_do_not_break_task_report(tmp_path, bad):
    write_log(tmp_path, [bad])
    report = timing_report(tmp_path)['owner_startup']
    assert report['status'] == 'invalid'
    assert report['invalid_records'] == 1
    assert report['observed_span_s'] is None


@pytest.mark.parametrize('case', ['duplicate', 'restart', 'missing_stage', 'overlap', 'after_failure'])
def test_ambiguous_startups_are_not_combined_into_one_duration(tmp_path, case):
    rows = records()
    if case == 'duplicate': rows.insert(1, dict(rows[0]))
    if case == 'restart': rows += records()
    if case == 'missing_stage': del rows[1]
    if case == 'overlap': rows[1]['since_start_s'] = 1.1
    if case == 'after_failure': rows[0]['complete'] = False
    write_log(tmp_path, rows)
    report = owner_startup_report(tmp_path)
    assert report['status'] == 'invalid'
    assert report['measured_stage_s'] is None


def test_malformed_utf8_json_and_large_log_are_bounded(tmp_path):
    path = tmp_path / 'owner.log'
    path.write_bytes(b'F1 STARTUP {\xff}\nF1 STARTUP {\n')
    assert owner_startup_report(tmp_path)['invalid_records'] == 2
    write_log(tmp_path, records())
    with path.open('ab') as stream:
        stream.write(b'x' * (MAX_LOG_BYTES + 100))
    report = owner_startup_report(tmp_path)
    assert report['scan_truncated'] is True
    assert report['status'] == 'complete'
    path.write_bytes(b'x' * (MAX_LOG_BYTES + 100))
    assert owner_startup_report(tmp_path)['status'] == 'unavailable'


def test_producer_output_roundtrips_without_control_startup(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'DAgger/F1/src'))
    from f1dagger.startup_timing import StartupTimings
    ticks = iter(range(17))
    rows = []
    timer = StartupTimings(enabled=True, clock=lambda: next(ticks), emit=rows.append)
    for stage in STAGES:
        with timer.stage(stage):
            pass
    write_log(tmp_path, rows)
    report = owner_startup_report(tmp_path)
    assert report['status'] == 'complete'
    assert report['measured_stage_s'] == 8
    assert report['observed_span_s'] == 16


def test_deeply_nested_corrupt_diagnostic_does_not_abort_review(tmp_path):
    (tmp_path / 'owner.log').write_text('F1 STARTUP ' + '[' * 10000 + ']' * 10000 + '\n')
    assert timing_report(tmp_path)['owner_startup']['status'] == 'invalid'
