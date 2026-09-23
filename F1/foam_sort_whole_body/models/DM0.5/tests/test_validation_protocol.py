import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import numpy as np
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from f1_training.validation_protocol import build_selection, paired_seed, paired_comparison, macro_summary, rank_candidates
from f1_training.validation_metrics import extra_metrics, seed_dispersion


def test_midpoints_use_valid_start_list_and_keep_short_recordings():
    eps = [SimpleNamespace(episode='a', starts=np.array([2,3,4,20,21,22,23,24])),
           SimpleNamespace(episode='b', starts=np.array([5,30]))]
    release = SimpleNamespace(for_split=lambda split: eps)
    protocol = build_selection(release, windows_per_episode=4, generation_seeds=[11,22], flow_windows_per_episode=16, flow_seeds=[1,2,3])
    assert [s['start'] for s in protocol['samples'] if s['episode']=='a'] == [3,20,22,24]
    assert [s['start'] for s in protocol['samples'] if s['episode']=='b'] == [5,30]
    assert len(protocol['flow_samples']) == 10
    assert protocol['batch_size'] == 1
    assert paired_seed(11,'a',20,'generation') == paired_seed(11,'a',20,'generation')
    assert len({paired_seed(11,'a',20,s) for s in ('flow','generation')}) == 2
    assert paired_seed(11,'a',20,'generation') != paired_seed(11,'a',21,'generation')


def report(errors):
    episodes = {}
    for name,error in errors.items():
        metric = {'left_position_mm':{'mean_over_available_windows_and_seeds':error}}
        episodes[name] = {'mean_fm_loss':error, 'evaluated_windows_times_seeds':8,'ik_rejected_prefixes':0,
                          '20':copy.deepcopy(metric),'50':copy.deepcopy(metric)}
    return {'comparison_contract':{'frozen_data':'same'},'episodes':episodes}


def test_paired_bootstrap_is_episode_paired_not_window_weighted():
    left,right = report({'a':1,'b':3,'c':5}),report({'a':2,'b':4,'c':6})
    result = paired_comparison(left,right,repeats=1000,seed=42)['metrics']['h20/left_position_mm']
    assert result['mean_delta_left_minus_right'] == -1
    assert result['ci95_low'] == result['ci95_high'] == -1
    assert result['left_episode_win_rate'] == 1
    assert macro_summary(left)['h20/left_position_mm']['mean'] == 3
    right['comparison_contract']['frozen_data'] = 'other'
    with pytest.raises(ValueError,match='comparison contracts'):
        paired_comparison(left,right,repeats=10,seed=42)


def test_missing_metric_never_wins_by_omission():
    left,right=report({'a':None,'b':1}),report({'a':1,'b':2})
    result=paired_comparison(left,right,repeats=100,seed=42)
    assert result['metrics']['h20/left_position_mm']['status']=='incomplete_metric'


def test_fieldwise_horizon_and_base_derivative_units():
    truth=np.zeros((50,23));truth[:,[3,7,12,16]]=1
    pred=truth.copy();pred[:,0]=np.arange(50)/30;pred[:,20]=np.arange(50)/30
    result=extra_metrics(pred,truth,50)
    assert result['left_position_at_h1_mm']==0
    assert result['left_position_at_h20_mm']==pytest.approx(19/30*1000)
    assert result['left_translation_velocity_rmse_m_s1']==pytest.approx(1/np.sqrt(3))
    assert result['base_vx_acceleration_rmse']==pytest.approx(1)
    assert result['base_vx_jerk_rmse']==pytest.approx(0,abs=1e-10)
    dispersion=seed_dispersion([truth,pred])
    assert dispersion['h20/left_position_mm']==pytest.approx(np.arange(20).mean()/30*1000)
    assert not any('total' in k for k in dispersion)


def full_report(arm, gripper, base):
    r = report({'a':arm, 'b':arm})
    for ep in r['episodes'].values():
        values = dict(left_position_mm=arm, right_position_mm=arm,
                      left_rotation_deg_valid_only=arm, right_rotation_deg_valid_only=arm,
                      left_gripper_mae=gripper, right_gripper_mae=gripper,
                      future_k1_base_mae=[base,base,base], nonfinite_elements=0,
                      left_rotation_invalid_steps=0, right_rotation_invalid_steps=0)
        ep['20'] = {k:{'mean_over_available_windows_and_seeds':v, 'available':8, 'total':8}
                    for k,v in values.items()}
    return r


def test_confirmed_weights_favor_arms_without_mixing_units():
    reports = {'arms_better':full_report(1,10,10), 'base_better':full_report(2,1,1)}
    weights = {'arms':.8, 'grippers':.1, 'base':.1}
    result = rank_candidates(reports, weights=weights)
    assert result['rows']['arms_better']['score'] == pytest.approx(.2)
    assert result['rows']['base_better']['score'] == pytest.approx(.8)
    reports['tie'] = copy.deepcopy(reports['arms_better'])
    result = rank_candidates(reports, weights=weights)
    assert result['rows']['arms_better']['score'] == result['rows']['tie']['score']
    assert not result['automatic_selection']


def test_invalid_or_partial_metrics_cannot_receive_optimistic_rank():
    reports = {'good':full_report(2,2,2), 'invalid':full_report(0,0,0), 'partial':full_report(1,1,1)}
    reports['invalid']['episodes']['a']['20']['left_rotation_invalid_steps']['mean_over_available_windows_and_seeds'] = 1
    reports['partial']['episodes']['b']['20']['left_position_mm']['available'] = 7
    result = rank_candidates(reports, weights={'arms':.8,'grippers':.1,'base':.1})
    assert result['rankable_pool'] == ['good']
    assert all(row['score'] is None for row in result['rows'].values())
    assert result['rows']['partial']['status'] == 'needs_review'
