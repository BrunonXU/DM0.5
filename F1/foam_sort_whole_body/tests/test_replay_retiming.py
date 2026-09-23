from pathlib import Path
import copy,json,sys,tempfile,unittest,math
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'robot'))
from f1_replay.bundle import load_bundle
from f1_replay.retiming import apply_retiming
from f1_replay.runtime import validate_actions,ReplayError
T=Path(__file__).resolve().parents[1]
R=T/'results/replay_retiming_057_20260916'
P=T/'results/deployments/20260916-replay-runtime-fix-v1/payload/runtime/replay_bundles/base_relabel_v3_clip_20260916/bundles/all_data_3_derived_episode_000057'
class RetimingTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.parent=load_bundle(P);cls.plan=json.loads((R/'retiming_057.json').read_text());cls.profile=json.loads((R/'replay_f1.json').read_text())
 def build(self,plan=None):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'plan.json';p.write_text(json.dumps(plan or self.plan));return apply_retiming(self.parent,p)
 def test_full_candidate_integrals_acceleration_and_rate(self):
  b=self.build();validate_actions(b,self.profile)
  self.assertEqual(len(b.frames),1357);self.assertAlmostEqual(b.duration_ns/1e9,45.233333333)
  prev=(0,0,0)
  for f in b.frames:
   now=f.action[16:]
   self.assertLessEqual(max(abs(x-y)*30 for x,y in zip(now,prev)),.13+1e-8)
   self.assertLessEqual(max(map(abs,now)),.65)
   prev=now
  for seg in b.manifest['retiming']['mapping']:
   frames=b.frames[seg['output_start_index']:seg['output_stop_index_exclusive']]
   for axis in range(2):self.assertAlmostEqual(sum(f.action[16+axis]/30 for f in frames),seg['distance_xy_m'][axis],places=10)
  self.assertLessEqual(max(map(abs,prev))*30,.13+1e-8)
 def test_shared_upper_mapping_and_source_identity(self):
  b=self.build();self.assertEqual(b.frames[0].state,self.parent.frames[0].state)
  self.assertEqual(b.frames[-1].action,self.parent.frames[-1].action)
  self.assertFalse(b.manifest['training_approved']);self.assertEqual(b.manifest['parent_manifest_sha256'],self.parent.sha256)
  for seg in b.manifest['retiming']['mapping']:
   k=seg['output_start_index'];a=seg['source_start_index']
   self.assertEqual(b.frames[k].action[:16],self.parent.frames[a].action[:16])
   n=seg['output_stop_index_exclusive']-k
   if n>1:
    j=n//2;source=a+j*(seg['source_stop_index_exclusive']-a)/n;lo=int(source);u=source-lo
    for c in range(16):
     hi=min(lo+1,len(self.parent.frames)-1)
     expected=self.parent.frames[lo].action[c]*(1-u)+self.parent.frames[hi].action[c]*u
     self.assertAlmostEqual(b.frames[k+j].action[c],expected,places=10)
 def test_binding_required(self):
  p=copy.deepcopy(self.plan);p['parent_manifest_sha256']='0'*64
  with self.assertRaises(ReplayError):self.build(p)
  p=copy.deepcopy(self.profile);p.pop('base_controller_expected_parameters')
  with self.assertRaises(ReplayError):validate_actions(self.build(),p)
 def test_unsafe_or_ambiguous_plan_rejected(self):
  for change in ['overlap','too_fast','distance','nan','missing_motion']:
   p=copy.deepcopy(self.plan)
   if change=='overlap':p['phases'][1]['start_index']=0
   elif change=='too_fast':p['phases'][0]['output_intervals']=38
   elif change=='distance':p['phases'][0]['delta_xy_m']=[-.4,0]
   elif change=='nan':p['limits']['acceleration']=float('nan')
   else:p['phases']=p['phases'][1:]
   with self.subTest(change=change),self.assertRaises((ReplayError,ValueError)):self.build(p)
 def test_parent_unchanged(self):
  before=(P/'frames.jsonl').read_bytes();self.build();self.assertEqual(before,(P/'frames.jsonl').read_bytes())
if __name__=='__main__':unittest.main()
