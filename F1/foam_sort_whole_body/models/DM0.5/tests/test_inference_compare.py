"""Paired HTTP diagnostics select by truth and never dispatch hardware."""
from pathlib import Path
import sys
import threading

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from f1_inference.compare import prediction_metrics, run_pair, select_samples, summarize
from f1_inference.server import action_response, validate_observation
from f1_policy_client.observation import Observation


def report_fixture():
    records, samples = [], []
    for episode in range(12):
        for start in (0, 10):
            item = {'episode': f'episode-{episode:03d}', 'start': start}
            samples.append(item)
            records.append({**item, 'metrics': {'20': {'base_known_steps': 20 if start == 0 else 0,
                                                      'moving_steps': 20 if episode < 6 else 0,
                                                      'left_position_mm': 100-episode}}})
    return {'selection': {'samples': samples}, 'records': records + records}


def sample_fixture():
    state = (0.,)*14+(50., 100., 0., 0., 0.)
    truth = np.zeros((50, 23))
    truth[:, [3, 7, 12, 16]] = 1
    truth[:, 18:20] = [50, 100]
    return {'selection': {'episode': 'episode-a', 'start': 0, 'stratum': 'stopped'},
            'observation': Observation(state, (0.,)*5, {}, 1, {}),
            'truth': truth, 'action_mask': np.ones((50, 32), bool), 'state_mask': np.ones(19, bool)}


def test_balanced_distinct_known_base_selection_ignores_prediction_quality():
    report = report_fixture()
    selected = select_samples(report, 8)
    assert len(selected) == len({row['episode'] for row in selected}) == 8
    assert sum(row['stratum'] == 'moving' for row in selected) == 4
    assert all(row['start'] == 0 for row in selected)
    for record in report['records']:
        record['metrics']['20']['left_position_mm'] = float('nan')
    assert select_samples(report, 8) == selected
    for record in report['records']:
        record['metrics']['20']['moving_steps'] = 0
    with pytest.raises(ValueError, match='moving and stopped'):
        select_samples(report, 8)


def test_metrics_keep_unclipped_gripper_and_wz_values():
    sample = sample_fixture()
    prediction = sample['truth'].copy()
    prediction[:, 0] = .002
    prediction[0, 19] = 100.125
    prediction[0, 22] = -.01
    metrics, bounds = prediction_metrics({'diagnostics': {'physical_action23': prediction.tolist()}}, sample)
    assert metrics['left_position_mm'] == pytest.approx(2)
    assert bounds['right_gripper']['max'] == 100.125
    assert bounds['right_gripper']['outside_0_100_elements'] == 1
    assert bounds['wz']['abs_max'] == .01 and bounds['wz']['nonzero_elements'] == 1


def test_parallel_pair_uses_same_seed_and_sample_then_summarizes(tmp_path):
    sample = sample_fixture()
    barrier = threading.Barrier(2, timeout=2)
    seen = []
    bindings = {'5k': {'checkpoint': '5k'}, '20k': {'checkpoint': '20k'}}

    class Client:
        def __init__(self, label):
            self.label = label

        def infer(self, observation, request_id, *, seed):
            seen.append((self.label, observation.captured_ns, seed, observation.state19))
            barrier.wait()
            obs = validate_observation({'contract': 'f1_foam_inference_v1', 'request_id': request_id,
                                        'observation_monotonic_ns': observation.captured_ns,
                                        'state19': list(observation.state19), 'seed': seed})
            solved = {'success': False, 'reason': 'fixture_rejection',
                      'inference_diagnostics': {'physical_action23': sample['truth'].tolist(),
                                                'timing_seconds': {'preprocess': .01, 'model': .2, 'ik': .03}}}
            return action_response(obs, solved, bindings[self.label])

    row = run_pair({name: Client(name) for name in bindings}, bindings, sample, 42, 'paired-test', tmp_path)
    assert all(result['status'] == 'rejected' for result in row['endpoints'].values())
    assert len(seen) == 2 and seen[0][1:] == seen[1][1:]
    assert all((tmp_path/f'paired-test_{name}.json').exists() for name in bindings)
    summary = summarize([row], list(bindings))
    assert summary['paired_windows'] == 1
    assert summary['endpoints']['5k']['ik_rejected'] == 1
    assert summary['paired_h20_deltas']['left_position_mm']['mean'] == 0
    assert summary['automatic_checkpoint_selection'] is False


def test_request_failure_is_visible_and_never_used_as_zero_error(tmp_path):
    class Failed:
        def infer(self, *args, **kwargs):
            raise TimeoutError('fixture timeout')
    row = run_pair({'5k': Failed(), '20k': Failed()}, {'5k': {}, '20k': {}}, sample_fixture(), 1, 'failed', tmp_path)
    summary = summarize([row], ['5k', '20k'])
    assert summary['paired_windows'] == 0
    assert summary['endpoints']['5k']['request_errors'] == 1
    assert not summary['endpoints']['5k']['h20_window_mean']
    assert 'TimeoutError' in row['endpoints']['5k']['error']
