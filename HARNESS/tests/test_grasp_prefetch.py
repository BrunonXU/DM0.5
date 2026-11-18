"""Concurrency and one-shot fallback tests: no processes, transports or motion."""
import sys,threading,time
from pathlib import Path
from types import SimpleNamespace
import pytest
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'scripts'))
from replay_grasp_prefetch import GraspPrefetch

class Intent(SimpleNamespace):
 def model_dump(self):return vars(self).copy()
class Obs:
 def model_dump(self):return {'id':'observed'}
class Worker:
 def __init__(self):self.calls=[];self.entered=threading.Event();self.release=threading.Event();self.closed=False;self.fail=None
 def dispatch(self,method,data):
  self.calls.append((method,data))
  if method=='prepare_grasp':
   self.entered.set();assert self.release.wait(2)
   if self.fail=='prepare':raise ValueError('offline_prepare_failed')
   return {'key':data['key']}
  if self.fail=='adopt':raise ValueError('offline_candidate_not_applicable')
  return dict(proposal={'request_id':data['intent']['request_id']},receipt={'checked':True})
 def close(self):self.closed=True;self.release.set()

def intent(skill='approach_grasp',epoch=1):return Intent(skill=skill,epoch=epoch,request_id=skill,owner_instance_id='owner',session_id='session',resume_request_id=None,verification_request_id=None)
def ready(p,w):
 assert w.entered.wait(1);w.release.set()
 p.slot['future'].result(timeout=1)

@pytest.mark.parametrize('mode',['normal','not_ready','scope','parent','epoch','prepare','adopt','cancel','closed','adoption_revoked'])
def test_one_slot_never_waits_for_unready_or_reuses_after_take(mode):
 w=Worker();allowed=[True];succeeded=[True];p=GraspPrefetch(w,lambda i:allowed[0],lambda i:succeeded[0])
 try:
  if mode=='closed':
   p.close();p.start(intent(),Obs(),[0]*22,{});assert not w.calls;return
  if mode in ('prepare','adopt'):w.fail=mode
  p.start(intent(),Obs(),[0]*22,{})
  if mode=='prepare':
   assert w.entered.wait(1);w.release.set()
   with pytest.raises(ValueError):p.slot['future'].result(timeout=1)
  elif mode!='not_ready':ready(p,w)
  if mode=='scope':allowed[0]=False
  if mode=='parent':succeeded[0]=False
  if mode=='cancel':p.invalidate('user_cancel')
  if mode=='adoption_revoked':
   original=w.dispatch
   def dispatch(method,data):
    result=original(method,data)
    if method=='adopt_grasp':allowed[0]=False
    return result
   w.dispatch=dispatch
  begin=time.monotonic();result=p.take(intent('grasp',epoch=2 if mode=='epoch' else 1),Obs(),[0]*22,[0]*16)
  assert time.monotonic()-begin<.1
  assert (result is not None)==(mode=='normal')
  assert p.take(intent('grasp'),Obs(),[0]*22,[0]*16) is None
 finally:p.close()
 assert w.closed


def test_parent_cannot_be_replaced_while_slot_exists_and_unrelated_skill_cannot_consume():
 w=Worker();p=GraspPrefetch(w,lambda i:True,lambda i:True)
 try:
  p.start(intent(),Obs(),[0]*22,{})
  first=p.slot;p.start(intent(),Obs(),[0]*22,{})
  assert p.slot is first
  assert p.take(intent('transfer_above_hole'),Obs(),[0]*22,[0]*16) is None and p.slot is first
  ready(p,w)
  assert p.take(intent('grasp'),Obs(),[0]*22,[0]*16) is not None
  assert len([c for c in w.calls if c[0]=='prepare_grasp'])==1
 finally:p.close()


def test_invalidated_running_job_cannot_queue_another_speculation():
 w=Worker();p=GraspPrefetch(w,lambda i:True,lambda i:True)
 try:
  p.start(intent(),Obs(),[0]*22,{})
  assert w.entered.wait(1)
  p.invalidate('cancel')
  p.start(intent(),Obs(),[0]*22,{})
  assert p.slot is None and len(w.calls)==1
  w.release.set();p.future.result(timeout=1)
  assert not any(r['kind']=='adopted' for r in p.records)
 finally:p.close()
