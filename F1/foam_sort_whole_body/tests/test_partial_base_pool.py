import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from build_partial_base_pool import build_partial


def example():
    t = np.arange(360)/30
    p = dict(time_ns=(t*1e9).astype(np.int64), state=np.zeros((360, 21)),
             action=np.zeros((360, 19)), state_valid=np.ones((360, 21), bool),
             action_candidate_valid=np.ones((360, 19), bool),
             observation_candidate_valid=np.ones(360, bool), chunk_start_candidate_valid=np.ones(360, bool))
    bounds=[]
    for role,a,b in [('back_out',2.,4.),('right_out',6.,8.)]:
        for key,value in [('start_s',a),('end_s',b)]:
            bounds.append(dict(role=role,boundary=key,primary=dict(status='bounded_visual_proposal',
                estimate_s=value,envelope_s=[value-.1,value+.1]),stable_under_pixel_thresholds=True))
    pairs=[dict(start_s=a,end_s=b,status='stationary_support') for a,b in [(0,2),(4,6),(8,12)]]
    return dict(boundaries=bounds),pairs,p,t


def test_missing_other_stages_keeps_supported_stages_and_windows():
    c,ps,p,t=example();d,r=build_partial(c,ps,p,t,11.)
    assert len(r['motion_stages'])==2 and len(r['unresolved_stages'])==3
    assert r['valid_starts']>200 and r['transition_starts']>0
    assert np.all(d['action'][t>=11,16:]!=d['action'][t>=11,16:])
    assert np.array_equal(d['action'][:,:16],p['action'][:,:16])


def test_conflict_masks_only_local_interval_and_crossing_windows():
    c,ps,p,t=example();ps.append(dict(start_s=2.7,end_s=2.9,status='stationary_support'))
    d,r=build_partial(c,ps,p,t,11.)
    assert np.isnan(d['action'][(t>=2.7)&(t<2.9),16:]).all()
    assert np.isfinite(d['action'][(t>=6)&(t<8),16:]).all()
    for i in np.flatnonzero(d['chunk_start_candidate_valid']):
        assert d['action_candidate_valid'][i:i+50].all()
        assert np.isfinite(d['action'][i:i+50]).all()


def test_unresolved_motion_not_filled_with_zero_and_parent_masks_preserved():
    c,ps,p,t=example();c['boundaries'][0]['stable_under_pixel_thresholds']=False
    p['chunk_start_candidate_valid'][20]=False;p['action_candidate_valid'][200,0]=False
    d,r=build_partial(c,ps,p,t,11.)
    assert np.isnan(d['action'][(t>2)&(t<4),16:]).all()
    assert not d['chunk_start_candidate_valid'][20]
    assert not d['chunk_start_candidate_valid'][151:201].any()
    assert (d['action_candidate_valid']<=p['action_candidate_valid']).all()


def test_gripper_motion_clash_remains_unknown_not_fake_stationary():
    c,ps,p,t=example();p['action'][80:100,14]=100
    d,r=build_partial(c,ps,p,t,11.)
    assert r['gripper_motion_conflict_frames']==2
    assert np.isnan(d['action'][[80,100],16:]).all()
    assert np.array_equal(d['action'][:,:16],p['action'][:,:16])


def test_overlapping_phases_do_not_destroy_unrelated_stationary_region():
    c,ps,p,t=example();c['boundaries'][2]['primary']['estimate_s']=3.
    d,r=build_partial(c,ps,p,t,11.)
    assert not r['motion_stages']
    assert np.isfinite(d['action'][t<2,16:]).all()
    assert np.isnan(d['action'][(t>2)&(t<4),16:]).all()
