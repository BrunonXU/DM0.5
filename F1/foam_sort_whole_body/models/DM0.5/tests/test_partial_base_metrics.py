import numpy as np
from f1_training.evaluation import physical_metrics

def test_generation_does_not_receive_future_label_availability():
    import torch
    from f1_training.evaluation import generation_inputs
    action = torch.zeros(1, 50, 32)
    mask = torch.ones_like(action, dtype=torch.bool)
    mask[..., 23:] = False
    mask[:, 10:20, 20:23] = False
    original = mask.clone()
    inputs = generation_inputs({'action': action, 'action_mask': mask,
                                'input_ids': torch.ones(1, 10, dtype=torch.long)})
    assert 'action' not in inputs
    assert inputs['action_mask'][..., :23].all()
    assert not inputs['action_mask'][..., 23:].any()
    assert torch.equal(mask, original)  # FM and metrics retain original supervision.


def test_unknown_base_does_not_become_stop_or_dynamic_error():
    truth = np.zeros((50, 23))
    truth[:, [3, 7, 12, 16]] = 1  # two valid identity row-6D rotations
    prediction = truth.copy()
    mask = np.ones((50, 32), bool); mask[:, 23:] = False
    mask[10:15, 20:23] = False
    prediction[10:15, 20:23] = 100
    state_mask = np.ones(19, bool); state_mask[16:] = False
    metrics = physical_metrics(prediction, truth, np.zeros(19), mask, state_mask)
    for horizon in (20, 50):
        r = metrics[str(horizon)]
        assert r['base_unknown_steps'] == 5
        assert r['stopped_steps'] == horizon - 5
        assert r['vx_mae'] == r['vx_rmse'] == 0
        assert r['base_vx_acceleration_rmse'] == r['base_vx_jerk_rmse'] == 0
        assert r['hold_current_base_mae'] is None
        assert r['left_position_mm'] == 0


def test_all_unknown_base_has_no_metrics_but_retains_arm_metrics():
    truth = np.zeros((50, 23)); truth[:, [3, 7, 12, 16]] = 1
    mask = np.ones((50, 32), bool); mask[:, 20:] = False
    r = physical_metrics(truth, truth, np.zeros(19), mask)['50']
    assert r['vx_mae'] is None and r['zero_base_mae'] is None
    assert r['moving_steps'] == r['stopped_steps'] == 0
    assert r['left_position_mm'] == 0 and r['right_gripper_mae'] == 0
