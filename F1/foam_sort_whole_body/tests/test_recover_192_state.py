import copy,importlib.util,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from recover_192_control import verify_state
from f1_replay.runtime import ReplayError

def report():
 arm={'current_state':1,'error_code':0,'drag_type':0,'feedback_command_deg':[0]*7,'sdk_input_target_deg':[0]*7,'joint_position_deg':[0]*7}
 return {'pid':123,'frame_changed':{'left':True,'right':True},'samples':[{'left':copy.deepcopy(arm),'right':copy.deepcopy(arm)} for _ in range(6)]}

def test_existing_error_only_allowed_before_recovery():
 r=report()
 for s in r['samples']:s['left'].update(current_state=100,error_code=2)
 verify_state(r,ready=False,held=True)
 with pytest.raises(ReplayError,match='不会重复清错'):verify_state(r,ready=True)

@pytest.mark.parametrize('issue',['right_error','other_error','stale','old_target','controller_old_target','drag'])
def test_recovery_rejects_unexpected_state_or_pending_motion(issue):
 r=report()
 if issue=='right_error':r['samples'][0]['right']['error_code']=2
 if issue=='other_error':r['samples'][0]['left'].update(current_state=100,error_code=99)
 if issue=='stale':r['frame_changed']['left']=False
 if issue=='old_target':r['samples'][0]['left']['sdk_input_target_deg'][3]=30
 if issue=='controller_old_target':r['samples'][0]['left']['feedback_command_deg'][3]=30
 if issue=='drag':r['samples'][0]['left']['drag_type']=1
 with pytest.raises(ReplayError):verify_state(r,ready=False,held=True)

def test_normal_position_state_and_current_hold_accepted():
 verify_state(report(),ready=True,held=True)

@pytest.mark.parametrize('initial_fault,post_fault,expected_resets,success',[(True,False,1,True),(True,True,1,False),(False,False,0,True)])
def test_one_recovery_reads_actual_sdk_before_success(monkeypatch,tmp_path,initial_fault,post_fault,expected_resets,success):
 import json
 from types import SimpleNamespace as N
 from contextlib import nullcontext
 import recover_192_control as m
 monkeypatch.setitem(sys.modules,'f1_msgs',N())
 monkeypatch.setitem(sys.modules,'f1_msgs.srv',N(SetControlMode=N(Request=N)))
 p=tmp_path/'profile.json';p.write_text(json.dumps({'expected_machine_id':Path('/etc/machine-id').read_text().strip()}))
 monkeypatch.setattr(m,'PROFILE',p);out=tmp_path/'run';out.mkdir();monkeypatch.setattr(m,'create_run',lambda:out)
 monkeypatch.setattr(m,'runtime_lock',lambda _:nullcontext());monkeypatch.setattr(m,'check_execution_host',lambda _:None)
 monkeypatch.setattr(m,'check_controller_parameters',lambda *_:None);monkeypatch.setattr(m,'wait_for_enter',lambda _:None)
 monkeypatch.setattr(m,'pump',lambda *_:None)
 monkeypatch.setattr(m,'stop_motion',lambda *_:{'zero_velocity_feedback_confirmed':True,'fresh_arm_hold_published':True})
 def state(fault):
  r=report()
  if fault:
   for s in r['samples']:s['left'].update(current_state=100,error_code=2)
  return r
 samples=iter([state(initial_fault),state(initial_fault),state(post_fault)])
 monkeypatch.setattr(m,'sample_sdk',lambda _:next(samples))
 calls=[]
 class Backend:
  def __init__(self,*_):self._node=self
  def open(self):pass
  def close(self):pass
  def check(self,**_):pass
  def poll(self,*_):pass
  def enable_commands(self):pass
  def read_controller_parameters(self):return {'parameters':{}}
  def destroy_client(self,*_):pass
  def create_client(self,*_):
   def call(req):
    calls.append(vars(req));return N(done=lambda:True,result=lambda:N(success=True,message='accepted'))
   return N(wait_for_service=lambda **_:True,call_async=call)
 monkeypatch.setattr(m,'RecoveryBackend',Backend)
 assert (m.main()==0)==success
 assert len(calls)==expected_resets
 if calls:assert calls==[{'target_arm':1,'mode':1}]
