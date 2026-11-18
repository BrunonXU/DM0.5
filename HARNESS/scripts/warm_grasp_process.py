"""Reusable offline grasp worker; reuse models, never per-run candidates.

Fixed task/fixture/model initialization only. A lease is local bookkeeping, not
motion authority. Existing GraspPrefetch consumes a lease as its worker. Closing
an idle lease clears the child slot; closing during computation kills the child
instead of keeping unknown native work for the next run. No automatic restart.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import threading
import time
import uuid

from f1_harness.contracts import HarnessError,Task,digest
from prepared_grasp import PreparedGraspService,SCOPE
from replay_planning_process import PlanningProcess,serve

ERROR_CODES=frozenset(('warm_grasp_run_active','warm_grasp_lease_sequence','warm_grasp_fixed_task_mismatch',
    'warm_grasp_lease_expired','warm_grasp_method_forbidden','warm_grasp_run_scope_mismatch',
    'warm_grasp_recipe_changed',
    'prepared_method_forbidden','prepared_parent_binding_mismatch','prepared_candidate_missing',
    'prepared_scope_mismatch','prepared_skill_not_eligible','prepared_candidate_excluded',
    'prepared_non_right_reference_changed','prepared_inactive_or_gripper_changed',
    'prepared_connection_budget_fallback','prepared_topology_changed','prepared_stage_changed'))


def recipe_identity(task,fixture):
    """The web request may change run name/recovery budget, not motion recipe."""
    task_value=task.model_dump();task_value.pop('task_id');task_value.pop('local_corrections')
    fixture_value=deepcopy(fixture)
    if isinstance(fixture_value.get('frame_chain'),dict):
        fixture_value['frame_chain'].pop('development_owner_config',None)
    return digest(dict(task=task_value,fixture=fixture_value))


def idle_clock_mapping():
    """Administrative requests need no reference to a finished replay's globals."""
    return dict(origin_ns=0,start_wall_ns=time.monotonic_ns())


class LeasedPreparedService:
    def __init__(self,service,numeric):
        self.worker=PreparedGraspService(service,numeric)
        self.task=service.task;self.template=service;self.numeric=numeric;self.active=None;self.serial=0
        self.recipe_id=recipe_identity(service.task,service.fixture)

    def dispatch(self,method,kwargs):
        if method=='begin_run':
            serial,token,scope=kwargs['serial'],kwargs['token'],kwargs['scope']
            if self.active is not None:raise HarnessError('warm_grasp_run_active')
            if type(serial) is not int or serial!=self.serial+1 or not isinstance(token,str) or not token:
                raise HarnessError('warm_grasp_lease_sequence')
            task=Task.model_validate(kwargs['task']) if 'task' in kwargs else self.template.task
            fixture=kwargs.get('fixture',self.template.fixture)
            if recipe_identity(task,fixture)!=self.recipe_id:raise HarnessError('warm_grasp_recipe_changed')
            if (set(scope)!=set(SCOPE) or scope['task_hash']!=digest(task)
                    or scope['task_id']!=task.task_id or scope['calibration_id']!=task.calibration_id):
                raise HarnessError('warm_grasp_fixed_task_mismatch')
            # Loading a current per-run owner/frame record is deliberate. A
            # normalized recipe hash alone cannot approve a new frame binding.
            if 'task' in kwargs or 'fixture' in kwargs:
                from f1_harness.skill_services import PlanService
                service=PlanService(task,deepcopy(fixture),Path('/unused-offline.sock'),clock=self.template.clock)
                service.numerics=self.template.numerics
                self.worker=PreparedGraspService(service,self.numeric)
            else:self.worker=PreparedGraspService(self.template,self.numeric)
            self.task=task
            self.serial=serial;self.active=dict(serial=serial,token=token,scope=deepcopy(scope))
            return dict(serial=serial,token=token,scope_sha256=digest(scope),candidate_cleared=True,execution_authorized=False)
        if self.active is None or any(kwargs.get(k)!=self.active[k] for k in ('serial','token')):
            raise HarnessError('warm_grasp_lease_expired')
        if method=='end_run':
            self.worker.prepared=None;self.active=None
            return dict(candidate_cleared=True,execution_authorized=False)
        if method!='lease_call' or kwargs.get('method') not in ('prepare_grasp','adopt_grasp'):
            raise HarnessError('warm_grasp_method_forbidden')
        inner=kwargs['kwargs'];intent=inner['parent_intent'] if kwargs['method']=='prepare_grasp' else inner['intent']
        if any(intent.get(k)!=v for k,v in self.active['scope'].items()):
            raise HarnessError('warm_grasp_run_scope_mismatch')
        return self.worker.dispatch(kwargs['method'],inner)


class WarmGraspProcess(PlanningProcess):
    WORKER_NAME='warm_grasp_process.py'
    METHODS=('begin_run','end_run','lease_call')

    def __init__(self,*,identity_files,assets=None,**kwargs):
        # Do not silently reuse editable sources under an old identity. These
        # files are supplied by the frozen replay provenance, excluding drivers
        # that are not executed. Models use the existing numerical asset binding.
        self.identities={str(Path(p).resolve()):hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in identity_files}
        if not self.identities:raise ValueError('warm_grasp_identity_files_required')
        self.leases_lock=threading.RLock();self.active_lease=None;self.lease_serial=0
        self.run_identities={}
        self.assets=None;self.asset_id=None
        if assets is not None:
            from audit_assets import load_assets
            self.assets=load_assets(assets);self.asset_id=self.assets.asset_id
            try:kwargs['spec']=self.assets.relocate(kwargs['spec'])
            except BaseException:self.assets.close();raise
        try:super().__init__(**kwargs)
        except BaseException:
            if self.assets is not None:self.assets.close()
            raise
        if not self.unchanged():self.close();raise RuntimeError('warm_grasp_source_changed_during_start')

    def unchanged(self):
        try:return ((self.assets is None or self.assets.unchanged()) and
                    all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h
                        for p,h in (self.identities|self.run_identities).items()))
        except OSError:return False

    def acquire(self,scope,*,clock_mapping=None,task=None,fixture=None,identity_files=()):
        begin=time.perf_counter()
        with self.leases_lock:
            if self.cleanup is not None:raise RuntimeError('warm_grasp_process_closed')
            if self.active_lease is not None:raise RuntimeError('warm_grasp_run_active')
            if not self.unchanged():self.close();raise RuntimeError('warm_grasp_source_changed')
            if (task is None)!=(fixture is None):raise ValueError('warm_grasp_task_and_fixture_required_together')
            if task is not None and not identity_files:raise ValueError('warm_grasp_run_identity_files_required')
            run_identities={str(Path(p).resolve()):hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in identity_files}
            if any(p in self.identities and h!=self.identities[p] for p,h in run_identities.items()):
                raise ValueError('warm_grasp_run_identity_conflicts_with_source')
            serial=self.lease_serial+1;token=uuid.uuid4().hex
            kwargs=dict(serial=serial,token=token,scope=scope)
            if task is not None:kwargs.update(task=task.model_dump(),fixture=fixture)
            try:
                receipt=super().dispatch('begin_run',kwargs)
                if receipt!=dict(serial=serial,token=token,scope_sha256=digest(scope),candidate_cleared=True,execution_authorized=False):
                    raise RuntimeError('warm_grasp_begin_identity_mismatch')
            except BaseException:self.close();raise
            self.run_identities=run_identities
            if not self.unchanged():self.close();raise RuntimeError('warm_grasp_run_changed_during_begin')
            # Administrative begin uses the previous valid clock. The next
            # replay clock starts on its first control tick, before any work.
            if clock_mapping is not None:self.clock_mapping=clock_mapping
            self.lease_serial=serial
            self.active_lease=GraspLease(self,serial,token,scope)
            self.active_lease.startup_s=time.perf_counter()-begin
            return self.active_lease

    def close(self):
        with self.leases_lock:
            if self.active_lease is not None:self.active_lease.closed=True
            self.active_lease=None
            self.run_identities={}
            self.clock_mapping=idle_clock_mapping
            super().close()
            if self.assets is not None:self.assets.close()


class GraspLease:
    def __init__(self,worker,serial,token,scope):
        self.worker,self.serial,self.token=worker,serial,token
        self.scope=deepcopy(scope);self.closed=False;self.busy=False

    @property
    def cleanup(self):
        return dict(lease_closed=self.closed,serial=self.serial,
                    worker_cleanup=self.worker.cleanup,worker_retained=self.closed and self.worker.cleanup is None)

    def dispatch(self,method,kwargs):
        worker=self.worker
        with worker.leases_lock:
            if self.closed or worker.active_lease is not self:raise RuntimeError('warm_grasp_lease_closed')
            if self.busy:raise RuntimeError('warm_grasp_lease_busy')
            if method not in ('prepare_grasp','adopt_grasp'):raise ValueError('warm_grasp_method_forbidden')
            if not worker.unchanged():worker.close();raise RuntimeError('warm_grasp_source_changed')
            self.busy=True
        try:
            result=worker.dispatch('lease_call',dict(serial=self.serial,token=self.token,method=method,kwargs=kwargs))
            with worker.leases_lock:
                if self.closed or worker.active_lease is not self:raise RuntimeError('warm_grasp_result_discarded')
                if not worker.unchanged():worker.close();raise RuntimeError('warm_grasp_source_changed')
            return result
        except HarnessError:
            # A matched, framed service rejection is not a broken transport.
            # Failed adoption already consumes the candidate in the child.
            raise
        except BaseException:
            worker.close();raise
        finally:
            with worker.leases_lock:self.busy=False

    def close(self):
        worker=self.worker
        with worker.leases_lock:
            if self.closed:return
            self.closed=True
            if self.busy:
                worker.close();return
            try:
                receipt=worker.dispatch('end_run',dict(serial=self.serial,token=self.token))
                if receipt!=dict(candidate_cleared=True,execution_authorized=False):
                    raise RuntimeError('warm_grasp_end_identity_mismatch')
            except BaseException:
                worker.close();raise
            finally:
                worker.active_lease=None;worker.run_identities={}
                worker.clock_mapping=idle_clock_mapping


if __name__=='__main__':
    import sys
    if len(sys.argv)!=3 or sys.argv[1]!='--worker':raise SystemExit('internal offline warm grasp worker only')
    serve(int(sys.argv[2]),service_wrapper=LeasedPreparedService,methods=WarmGraspProcess.METHODS,error_codes=ERROR_CODES)
