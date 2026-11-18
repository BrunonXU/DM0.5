"""Independent offline speculative worker and nonblocking single-slot coordinator."""
from concurrent.futures import ThreadPoolExecutor
import threading,time,uuid
from replay_planning_process import PlanningProcess,serve


class GraspProcess(PlanningProcess):
    WORKER_NAME='replay_grasp_prefetch.py'
    METHODS=('prepare_grasp','adopt_grasp')


class GraspPrefetch:
    def __init__(self,worker,current,parent_succeeded,*,delay_s=0.,transfer=False,transfer_delay_s=0.):
        self.worker,self.current,self.parent_succeeded=worker,current,parent_succeeded
        self.delay_s=delay_s;self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='offline-prefetch')
        self.lock=threading.Lock();self.slot=None;self.future=None;self.closed=False;self.records=[]
        self.transitions={'approach_grasp':('grasp','prepare_grasp','adopt_grasp')}
        if transfer:self.transitions['grasp']=('transfer_above_hole','prepare_transfer','adopt_transfer')
        self.transfer_delay_s=transfer_delay_s

    def _event(self,kind,**fields):
        with self.lock:self.records.append(dict(kind=kind,wall_ns=time.monotonic_ns(),**fields))

    def start(self,intent,observation,full,proposal):
        if intent.skill not in self.transitions or intent.resume_request_id or intent.verification_request_id:return
        child,prepare,adopt=self.transitions[intent.skill]
        with self.lock:
            if (self.closed or self.slot is not None or not self.current(intent)
                    or self.future is not None and not self.future.done()):return
            key=uuid.uuid4().hex
            def work():
                delay=self.transfer_delay_s if child=='transfer_above_hole' else self.delay_s
                if delay:time.sleep(delay)
                self._event('prepare_started',key=key,epoch=intent.epoch,parent_request=intent.request_id,skill=child)
                try:
                    value=self.worker.dispatch(prepare,dict(key=key,parent_intent=intent.model_dump(),
                        parent_proposal=proposal,observation=observation.model_dump(),full22=list(full)))
                except Exception as exc:
                    self._event('prepare_failed',key=key,error_type=type(exc).__name__,reason=str(exc))
                    raise
                self._event('prepare_finished',key=key,skill=child,result=value)
                return value
            future=self.pool.submit(work)
            self.future=future
            self.slot=dict(key=key,parent=intent,future=future,skill=child,adopt=adopt)

    def take(self,intent,observation,full,held):
        if intent.skill not in {v[0] for v in self.transitions.values()}:return None
        with self.lock:slot,self.slot=self.slot,None
        if slot is None:return None
        future=slot['future'];key=slot['key']
        if (intent.skill!=slot['skill'] or not self.current(intent) or not self.parent_succeeded(slot['parent'])
                or any(getattr(intent,k,None)!=getattr(slot['parent'],k,None) for k in
                       ('task_id','task_hash','session_id','owner_instance_id','epoch','calibration_id'))):
            future.cancel();self._event('discarded',key=key,reason='scope_or_parent_changed');return None
        if not future.done():
            future.cancel();self._event('fallback',key=key,skill=intent.skill,reason='not_ready');return None
        try:
            future.result()
            value=self.worker.dispatch(slot['adopt'],dict(key=key,intent=intent.model_dump(),
                observation=observation.model_dump(),full22=list(full),held16=list(held)))
            if not self.current(intent):self._event('discarded',key=key,reason='authority_changed_during_adoption');return None
            self._event('adopted',key=key,skill=intent.skill,request_id=intent.request_id,receipt=value['receipt'])
            return value['proposal']
        except Exception as exc:
            self._event('fallback',key=key,skill=intent.skill,reason=str(exc),error_type=type(exc).__name__)
            return None

    def invalidate(self,reason):
        with self.lock:slot,self.slot=self.slot,None
        if slot is not None:
            slot['future'].cancel();self._event('discarded',key=slot['key'],reason=reason)

    def close(self):
        with self.lock:self.closed=True
        self.invalidate('closed')
        self.worker.close()
        self.pool.shutdown(wait=True,cancel_futures=True)


if __name__=='__main__':
    import sys
    from prepared_grasp import PreparedGraspService
    if len(sys.argv)!=3 or sys.argv[1]!='--worker':raise SystemExit('internal offline prefetch worker only')
    serve(int(sys.argv[2]),service_wrapper=PreparedGraspService,methods=GraspProcess.METHODS)
