"""Reproducible isolated 058 mask revision and 003 replay-only dynamic candidate."""
from pathlib import Path
import hashlib,json,math,shutil,sys
import numpy as np
from isolate_uncertain_interval import isolate
T=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(T/'robot'),str(T/'models/DM0.5')]
from f1_replay.bundle import load_bundle
from f1_replay.retiming import apply_retiming
from f1_replay.runtime import validate_actions
from f1_data.reader import Episode
OUT=T/'results/offline_repair_003_058_20260917'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,obj):
 with path.open('x') as f:json.dump(obj,f,ensure_ascii=False,indent=2,allow_nan=False);f.write('\n')
def arrays(path):
 with np.load(path,allow_pickle=False) as z:return {k:z[k].copy() for k in z.files}


def repair058():
 case='all_data_3_derived_episode_000058';parent=T/'results/base_relabel_v3_autonomous/cases'/case/'composed/episode.npz'
 phase=T/'results/base_relabel_v3_autonomous/cases'/case/'phase_review.json';origin=int(json.loads(phase.read_text())['source_origin_ns'])
 before=arrays(parent);original_hash=sha(parent)
 # Earliest reviewed release command to end of the pre-existing unknown background interval.
 result,bad=isolate(before,origin_ns=origin,start_ns=16_433_333_333,stop_ns=16_976_000_000,horizon=50)
 mutable={'action_candidate_valid','base_timing_reviewed','future_signal_action_candidate_valid','joint_frame_candidate_valid','chunk_start_candidate_valid','training_approved'}
 for k in before:
  if k not in mutable:assert np.array_equal(before[k],result[k],equal_nan=True) if before[k].dtype.kind in 'fc' else np.array_equal(before[k],result[k])
  elif before[k].dtype==bool:assert not (result[k]&~before[k]).any()
 dst=OUT/'data_058';dst.mkdir();np.savez_compressed(dst/'episode.npz',**result)
 meta=json.loads(parent.with_suffix('.json').read_text())
 evidence=T/'results/temporal_acceptance_20260917/visual_findings.md'
 review=dict(scope='uncertainty_isolation_not_reconstructed_truth',source_parent=str(parent),source_parent_sha256=original_hash,
  evidence={'path':str(evidence),'sha256':sha(evidence)},interval_s=[16.433333333,16.976],masked_indices=np.flatnonzero(bad).tolist(),
  action_labels_changed=False,camera_and_time_changed=False,masks_never_relaxed=True,training_approved=False,
  original_chunk_starts=int(before['chunk_start_candidate_valid'].sum()),revised_chunk_starts=int(result['chunk_start_candidate_valid'].sum()))
 meta['uncertainty_revision']=review;meta['version']='base_v3_058_uncertainty_isolation_20260917';meta['training_approved']=False
 # Keep inherited counts as parent history rather than masquerading as revised counts.
 meta['parent_counts_before_uncertainty_revision']={k:meta.pop(k) for k in ['composition','chunk_candidate_starts','joint_frame_candidate_valid_frames','continuous_windows'] if k in meta}
 meta['chunk_candidate_starts']=int(result['chunk_start_candidate_valid'].sum());meta['joint_frame_candidate_valid_frames']=int(result['joint_frame_candidate_valid'].sum())
 write(dst/'episode.json',meta)
 cache=T/'results/kinematics_validation_20260916/pose_cache'/case/'pose_cache.npz'
 cm=json.loads(cache.with_suffix('.json').read_text());assert cm['source_npz_sha256']==original_hash and cm['pose_cache_sha256']==sha(cache)
 # Cache is a numeric FK function of unchanged state/action/time; reader applies the revised masks.
 for k in ['state','action','time_ns']:np.testing.assert_array_equal(before[k],result[k])
 shutil.copy2(cache,dst/'pose_cache.npz');cm['source_path']=str(dst/'episode.npz');cm['source_npz_sha256']=sha(dst/'episode.npz')
 cm['mask_only_rebinding']={'parent_source_sha256':original_hash,'parent_cache_metadata_sha256':sha(cache.with_suffix('.json')),'unchanged_inputs':['state','action','time_ns'],'cache_bytes_unchanged':True}
 write(dst/'pose_cache.json',cm)
 old=Episode(parent,cache,allow_candidates=True);new=Episode(dst/'episode.npz',dst/'pose_cache.npz',allow_candidates=True)
 for t in new.starts:
  assert not bad[t:t+50].any();new.sample(int(t))
 assert set(new.starts)<=set(old.starts) and sha(parent)==original_hash
 review['reader_windows_before']=len(old.starts);review['reader_windows_after']=len(new.starts)
 review['reader_all_remaining_numeric_windows_checked']=True
 write(dst/'verification.json',review)
 return review


def candidate003():
 case='all_data_2_derived_episode_000003'
 parent=T/'results/base_home_delivery_20260917/bundles'/case;b=load_bundle(parent)
 phasefile=T/'results/base_relabel_v3_autonomous/cases'/case/'phase_review.json';phase=json.loads(phasefile.read_text())
 paramsfile=T/'results/base_controller_review_20260916/parameters_decoded.json';params=json.loads(paramsfile.read_text())['controller_node']
 limits={k:params[n] for k,n in [('acceleration','max_acceleration.linear'),('deceleration','max_deceleration.linear'),('speed','max_speed.linear')]}
 offset=(b.frames[0].source_time_ns-int(phase['source_origin_ns']))/1e9
 plan=dict(schema='f1_replay_retiming_v1',scope='replay_diagnostic_not_training',episode=b.manifest['source']['episode'],parent_manifest_sha256=b.sha256,hz=30,limits=limits,phase_source_sha256=sha(phasefile),parameters_source_sha256=sha(paramsfile),phases=[])
 for s in phase['segments']:
  if s['kind']!='motion':continue
  a=round((s['start_s']-offset)*30);z=round((s['end_s']-offset)*30);distance=math.hypot(s['dx_m'],s['dy_m'])
  duration=max((z-a)/30,math.sqrt(6*distance/limits['acceleration']),math.sqrt(6*distance/limits['deceleration']),1.5*distance/min(limits['speed'],.65))
  plan['phases'].append(dict(id=s['id'],start_index=a,stop_index_exclusive=z,delta_xy_m=[s['dx_m'],s['dy_m']],output_intervals=math.ceil(duration*30)))
 dst=OUT/'replay_003';dst.mkdir();write(dst/'retiming.json',plan)
 profile=json.loads((T/'results/replay_retiming_057_20260916/replay_f1.json').read_text())
 profile['diagnostic_note']='003 offline replay-only candidate; historical parameter snapshot must be checked live. No training label replacement, no stop/arrival guarantee.'
 write(dst/'profile.json',profile)
 candidate=apply_retiming(b,dst/'retiming.json');validate_actions(candidate,profile)
 v=np.array([f.action[16:19] for f in candidate.frames]);acc=np.diff(np.vstack([np.zeros(3),v,np.zeros(3)]),axis=0)*30
 assert abs(acc).max()<=min(limits['acceleration'],limits['deceleration'])+1e-8
 for m in candidate.manifest['retiming']['mapping']:
  lo,hi=m['output_start_index'],m['output_stop_index_exclusive']
  np.testing.assert_allclose(v[lo:hi,:2].sum(axis=0)/30,m['distance_xy_m'],atol=1e-10)
 assert np.array_equal(candidate.frames[0].state,b.frames[0].state)
 report=dict(source_frames=len(b.frames),source_duration_s=b.duration_ns/1e9,candidate_frames=len(candidate.frames),candidate_duration_s=candidate.duration_ns/1e9,max_interval_velocity_change_m_s2=float(abs(acc).max()),limits=limits,training_approved=False,physical_verified=False,deployed=False,mapping=candidate.manifest['retiming']['mapping'])
 write(dst/'generated_manifest.json',candidate.manifest);write(dst/'verification.json',report)
 return report


if __name__=='__main__':
 OUT.mkdir(exist_ok=False)
 write(OUT/'summary.json',{'058':repair058(),'003':candidate003()})
 print((OUT/'summary.json').read_text())
