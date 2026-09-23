from pathlib import Path
import sys,json
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'robot'))
from f1_replay.bundle import load_bundle
from f1_replay.retiming import apply_retiming
from f1_replay.runtime import validate_actions
T=Path(__file__).resolve().parents[1]
R=T/'results/offline_repair_003_058_20260917'


def test_003_candidate_keeps_task_and_meets_boundaries():
 parent=load_bundle(T/'results/base_home_delivery_20260917/bundles/all_data_2_derived_episode_000003')
 result=apply_retiming(parent,R/'replay_003/retiming.json')
 validate_actions(result,json.loads((R/'replay_003/profile.json').read_text()))
 assert len(result.frames)==1387
 assert result.manifest['training_approved'] is False
 assert result.frames[0].state==parent.frames[0].state
 assert result.frames[-1].action==parent.frames[-1].action
 v=np.array([x.action[16:] for x in result.frames])
 assert abs(np.diff(np.vstack([np.zeros(3),v,np.zeros(3)]),axis=0)*30).max()<=.13+1e-8
 for m in result.manifest['retiming']['mapping']:
  a,b=m['output_start_index'],m['output_stop_index_exclusive']
  np.testing.assert_allclose(v[a:b,:2].sum(axis=0)/30,m['distance_xy_m'],atol=1e-10)
  if m['id']=='source_stationary':assert not v[a:b].any()
 # Release in the original stationary segment retains its original offset;
 # do not move the gripper independently to make this offline check pass.
 source_index=517 # original 17.733333s, relative start 0.5s
 m=next(x for x in result.manifest['retiming']['mapping'] if x['source_start_index']<=source_index<x['source_stop_index_exclusive'])
 assert m['id']=='source_stationary'
 k=m['output_start_index']+source_index-m['source_start_index']
 assert result.frames[k].action[:16]==parent.frames[source_index].action[:16]
