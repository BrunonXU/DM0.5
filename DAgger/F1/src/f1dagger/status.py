"""Latest-only status mailbox. No filesystem/network on the control thread."""
import json
import os
from pathlib import Path
import threading
import time
from .runtime import RuntimeReporter

class StatusPublisher:
    def __init__(self, path, engine, sink, binding, run_id, finish_event=None):
        self.path=Path(path); self.engine=engine; self.binding=binding; self.run_id=run_id
        self.finish_event=finish_event
        self.identity=dict(run_id=run_id,pid=os.getpid(),start_ticks=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])
        self.incoming=None; self.limit=None; self.error=None; self.stop=threading.Event()
        self.wake=threading.Event(); self.key=None; self.intervention=None; self.intervention_start=None
        self.reporter=RuntimeReporter(binding['execution_mode'],sink,emit=self.write,interval_ns=0)
        self.console=RuntimeReporter(binding['execution_mode'],sink)
        self.thread=threading.Thread(target=self.work,name='f1-status',daemon=True); self.thread.start()
    def update(self,incoming,limit=None):
        self.incoming=incoming; self.limit=limit
        key=(self.engine.supervisor.state.value,self.engine.supervisor.policy_epoch,self.engine.started)
        if key!=self.key:self.key=key;self.wake.set()
    def write(self,line,**_):
        fields=json.loads(line.removeprefix('F1 STATUS ')); e=self.engine
        if e.supervisor.intervention_id != self.intervention:
            self.intervention=e.supervisor.intervention_id; self.intervention_start=time.monotonic()
        fields.update(intervention_elapsed_s=None if not self.intervention else time.monotonic()-self.intervention_start,
                      runtime_fault=getattr(e, 'runtime_fault', None),
                      recording_failure=getattr(e, 'recording_failed', None),
                      intervention_id=e.supervisor.intervention_id,limit_reason=self.limit,
                      policy_stage='executing' if e.chunk is not None else 'requesting' if e.real_future else 'waiting',
                      policy_health=('failed' if str(e.supervisor.hold_reason or '').startswith('policy_failed:') else
                                     'validated' if e.chunk is not None or e.ready is not None else
                                     'requesting' if e.real_future is not None else
                                     'bound' if self.binding.get('model_id') else 'unconfigured'),
                      model_id=self.binding.get('model_id'),purpose=self.binding['purpose'])
        if hasattr(e, 'rlt_phase'):
            fields.update(rlt_phase=e.rlt_phase, rlt_cycle=e.rlt_cycle,
                          active_model_id=e.policy.model_id,
                          actor_version=e.rlt_policy.rlt_identity['actor_version'],
                          rlt_local_task=e.local_task.metadata(), ending_episode=e.ending_episode,
                          actor_update_error=getattr(e.rlt_policy, 'candidate_error', None))
        buttons = getattr(e, 'episode_buttons', None)
        if buttons:
            fields.update(episode_controls='f1_rlt_local_v4' if hasattr(e, 'local_task') else 'start_up_v2', episode_end_hint=buttons.end_hint,
                home_active=buttons.home_active,
                terminal_hold_seconds=None if buttons.hold_since is None else max(0., (time.monotonic_ns()-buttons.hold_since)/1e9))
        if hasattr(e, 'ik_state'):
            fields.update(ik_recovery_state=e.ik_state, ik_failures=e.ik_failures,
                          ik_retry_limit=e.ik_retry_limit)
        data=dict(schema='f1_hil_status_v1',pid=os.getpid(),
                  start_ticks=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19],
                  run_id=self.run_id,updated_unix=time.time(),updated_monotonic_ns=time.monotonic_ns(),
                  binding=self.binding,status=fields,capabilities=['finish_session_v1'] if self.finish_event is not None else [])
        self.path.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(data,allow_nan=False));tmp.replace(self.path)
    def work(self):
        while not self.stop.is_set():
            try:
                from .lifecycle import finish_requested
                if self.finish_event is not None and finish_requested(self.path.parent/'finish-request.json',self.identity):
                    self.finish_event.set()
                self.reporter.report(time.monotonic_ns(),self.engine,self.incoming)
                self.console.report(time.monotonic_ns(),self.engine,self.incoming)
            except Exception as exc:self.error=str(exc)
            self.wake.wait(.2);self.wake.clear()
    def close(self, failed=False):
        self.stop.set();self.wake.set();self.thread.join(2)
        if not self.thread.is_alive():
            try:
                data=json.loads(self.path.read_text())
                data.update(updated_unix=time.time(),updated_monotonic_ns=time.monotonic_ns())
                data['status'].update(state='failed' if failed else 'stopping',input_fresh=False)
                tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(data));tmp.replace(self.path)
            except (OSError,ValueError):pass

