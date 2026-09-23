import copy
from pathlib import Path
import sys
import numpy as np
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from f1_training.monitoring import configure_wandb, evaluation_scalars
from f1_training.evaluation import physical_metrics


def test_wandb_identity_resume_and_no_silent_offline(monkeypatch, tmp_path):
    for key in ('WANDB_MODE', 'WANDB_DISABLED', 'WANDB_RUN_ID', 'WANDB_RESUME', 'WANDB_LOG_MODEL'):
        monkeypatch.delenv(key, raising=False)
    import os
    # Track environment writes for restoration after this test.
    for key in ('WANDB_RUN_ID', 'WANDB_RESUME', 'WANDB_LOG_MODEL'):
        monkeypatch.setenv(key, '')
        monkeypatch.delenv(key)
    monkeypatch.setenv('WANDB_MODE', 'online')
    run = configure_wandb(tmp_path)
    assert run.startswith('f1-') and os.environ['WANDB_RESUME'] == 'never'
    assert configure_wandb(tmp_path, resume=True) == run
    assert os.environ['WANDB_RESUME'] == 'must'
    assert os.environ['WANDB_LOG_MODEL'] == 'false'
    with pytest.raises(ValueError, match='conflicts'):
        configure_wandb(tmp_path / 'another_run')
    monkeypatch.setenv('WANDB_MODE', 'offline')
    with pytest.raises(ValueError, match='online W&B'):
        configure_wandb(tmp_path)


def test_wandb_evaluation_equal_episode_mean_not_window_weighted():
    physical = np.zeros((50,23)); physical[:, [3,7,12,16]] = 1
    raw = physical_metrics(physical, physical, np.zeros(19))
    ep = {h: {k: {'mean_over_available_windows_and_seeds': v} for k,v in m.items()} for h,m in raw.items()}
    a,b = copy.deepcopy(ep),copy.deepcopy(ep)
    a.update(mean_fm_loss=2, evaluated_windows_times_seeds=1, ik_rejected_prefixes=1)
    b.update(mean_fm_loss=8, evaluated_windows_times_seeds=100, ik_rejected_prefixes=0)
    output = evaluation_scalars({'episodes': {'a':a, 'b':b}})
    assert output['eval_fm_loss_episode_mean'] == 5
    assert output['eval_ik_rejection_rate_episode_mean'] == .5
    assert output['eval_h20/stopped_vx_mae_episode_mean'] == 0
    assert 'eval_h20/moving_vx_mae_episode_mean' not in output
    assert not any('score' in k for k in output)
