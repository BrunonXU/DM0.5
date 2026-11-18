"""Trusted infra supervisor. GET is read-only; only an explicit POST creates a run."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from .evidence import EvidenceStore
from .web_snapshot import checked_request, freeze_site


class WorkspaceSupervisor:
    def __init__(self, config, *, processes=None):
        from .web_process import WorkspaceProcesses
        self.config = config
        self.root = Path(config['root'])
        self.runs_root = Path(config['runs_root'])
        self.processes = processes or WorkspaceProcesses
        self.lock = threading.RLock()
        self.current = None
        self.thread = None
        self.cancel = threading.Event()
        self.faulted = False
        self.recovery_of = None
        self.closing = False
        self.boot_client = None
        # Reuse the deployed workbench's run parser and redaction, never its execution proxy.
        spec = importlib.util.spec_from_file_location('_harness_workspace_records',
            self.root.parent / 'F1-4242/task_service.py')
        self.records = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.records)
        self.reader = self.records.TaskService(self.runs_root, offline=True)
        # Restarting the service must not silently clear an unconfirmed stop.
        previous=[]
        for run in self.runs_root.glob('web-*'):
            if run.is_symlink():continue
            try:previous.append((self._metadata(run),run))
            except (OSError,ValueError):continue
        if previous:
            metadata,run=max(previous,key=lambda item:item[0].get('started_at',0))
            uncertain=(metadata.get('restart_requires_stationary',False)
                or metadata.get('status') in ('starting','running','stopping','unknown')
                and not metadata.get('stop_confirmation'))
            if not metadata.get('stop_confirmation') and not uncertain:
                try:uncertain='"kind": "web_stop_unconfirmed"' in (run/'trace.jsonl').read_text()
                except OSError:pass
            if uncertain:
                self.faulted=True
                self.recovery_of=metadata['id']

    @staticmethod
    def run_id(name):
        return 'harness-' + hashlib.sha256(name.encode()).hexdigest()[:20]

    def _metadata(self, run):
        path = Path(run) / 'workspace-run.json'
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError('运行身份文件无效。')
        return json.loads(path.read_text())

    def _update(self, run, **values):
        with self.lock:
            value = self._metadata(run)
            value.update(values)
            temporary = Path(run) / '.workspace-run.tmp'
            temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
            temporary.replace(Path(run) / 'workspace-run.json')

    def _render(self, run, *, detail=False):
        metadata = self._metadata(run)
        parsed = self.reader._read_run(Path(run) / 'planner', detail=detail) if (Path(run) / 'planner').is_dir() else None
        parsed = parsed or dict(events=[], actions=[], outcome='unknown', result_verified=False)
        status = metadata['status']
        if (self.current != Path(run) or not self.thread or not self.thread.is_alive()) and status in (
                'starting', 'running', 'stopping'):
            status = 'unknown'
        end = metadata.get('ended_at', time.time())
        parsed.update(id=metadata['id'], label='单块完整流程 · ' + Path(run).name[-8:],
            status=status, source='hardware', started_at=metadata['started_at'],
            elapsed_s=end - metadata['started_at'], config=metadata['config'],
            summary=metadata.get('summary', ''), stop_confirmation=metadata.get('stop_confirmation'),
            result_verified=False)
        try:
            from .export_worker import job_path
            job = json.loads(job_path(run).read_text())
            parsed['export_state'] = job['state']
        except (OSError, ValueError, KeyError):
            parsed['export_state'] = 'not_queued'
        if detail:
            rows, warnings = self.reader._trace(run)
            extra = [dict(id=r['event_id'], kind=r['kind'], title=r.get('title', r['kind']),
                          message=self.records._text(r.get('reason', '')), level=r.get('level', 'info'),
                          time_s=(r['monotonic_ns']-metadata['started_ns'])/1e9,
                          detail=self.records._clean(r)) for r in rows]
            planner_rows, _ = self.reader._trace(Path(run)/'planner') if (Path(run)/'planner').is_dir() else ([],[])
            if planner_rows:
                offset=(min(r['monotonic_ns'] for r in planner_rows)-metadata['started_ns'])/1e9
                for event in parsed.get('events', []):
                    if isinstance(event.get('time_s'),(int,float)): event['time_s']+=offset
            parsed['events'] = sorted(extra + parsed.get('events', []),key=lambda e:e.get('time_s') or 0)[-500:]
            parsed['warnings'] = warnings + parsed.get('warnings', [])
        return parsed

    def state(self):
        with self.lock:
            busy = bool(self.thread and self.thread.is_alive())
            current = self._render(self.current) if self.current else None
            return dict(current_run=current, capabilities=dict(can_start=not busy and not self.closing,
                can_stop=busy, reason=('任务服务正在退出。' if self.closing else
                    '上次停止未确认；点击开始将重新准备，取得新控制端实测停稳证据后才授权新任务。' if self.faulted and not busy else
                    '本次配置已冻结；可随时请求停止。' if busy else
                    self.config.get('readiness_note') or
                    '点击开始执行完整单块任务，无需手柄；固定技能不调用模型API。')))

    def start(self, body):
        request = checked_request(body)
        with self.lock:
            if self.closing or self.thread and self.thread.is_alive():
                raise ValueError('已有任务正在执行或收尾，不重复启动。')
            run = self.runs_root / ('web-' + uuid.uuid4().hex)
            store = EvidenceStore(run)
            metadata = dict(schema=1, id=self.run_id(run.name), started_at=time.time(), started_ns=time.monotonic_ns(),
                status='starting', config=request['config'], config_revision=request['config_revision'],
                summary='正在准备完整任务。', request=request,
                restart_requires_stationary=self.faulted,recovery_of=self.recovery_of if self.faulted else None)
            store.write_json('workspace-run.json', metadata)
            store.event('web_task_requested', title='已收到网页开始请求', config_revision=request['config_revision'])
            self.current = run
            self.cancel = threading.Event()
            self.thread = threading.Thread(target=self._execute, args=(run, request, self.cancel), daemon=True)
            self.thread.start()
            return dict(run_id=metadata['id'], status='starting', motion_authorized=False)

    def stop(self, body):
        if not isinstance(body, dict) or set(body) != {'run_id'}:
            raise ValueError('停止请求须绑定本次运行。')
        with self.lock:
            if self.current is None or body['run_id'] != self.run_id(self.current.name):
                raise ValueError('当前运行身份已改变，未提交停止。')
            if not self.thread or not self.thread.is_alive():
                return dict(run_id=body['run_id'], status=self._metadata(self.current)['status'])
            self.cancel.set()
            self._update(self.current, status='stopping', summary='已请求停止，等待控制端保持和实测反馈。')
            return dict(run_id=body['run_id'], status='stopping', measured_stationary=False)

    @staticmethod
    def _binding(status, request_id, *, stopping=False):
        context, recording = status['perception_context'], status['recording_identity']
        scope=(status.get('stop_scope') or {}) if stopping else {}
        return dict(request_id=request_id, run_id=recording['owner_run_id'],
            owner_instance_id=status['owner_instance_id'], session_id=scope.get('session_id',context['session_id']),
            task_hash=status['task_hash'], epoch=status['epoch'])

    def _wait_status(self, processes, cancel, *, prepared=False, planner=None):
        deadline = time.monotonic() + 60
        last = '控制端准备中。'
        while time.monotonic() < deadline:
            processes.check()
            if planner is not None and planner.poll() is not None:
                raise RuntimeError('任务规划进程在准备完成前退出，请查看本次planner日志。')
            if cancel.is_set():
                raise InterruptedError('准备期间已取消。')
            try:
                status = processes.owner_call('status')
            except Exception as exc:
                last = str(exc)
            else:
                self._check_owner_status(processes, status)
                if not prepared or status.get('task_hash') and status.get('perception_context', {}).get('session_id'):
                    return status
            cancel.wait(.2)
        raise TimeoutError(last)

    @staticmethod
    def _check_owner_status(processes, status):
        expected = processes.expected_task_hash
        if (status.get('admitted_task_hash') != expected or status.get('web_control_enabled') is not True
                or status.get('task_hash') not in (None, expected)
                or getattr(processes, 'expected_owner_instance', None) not in (None, status.get('owner_instance_id'))):
            raise ValueError('控制端不属于本次冻结任务；未向其他运行发送授权或停止。')

    @staticmethod
    def _authority_failure(status, default='控制端已撤销本次运行授权。'):
        revoked=status.get('authority_revocation') or {}
        return '控制端中断：' + str(revoked.get('reason') or default)[:500]

    @staticmethod
    def _planner_failure(run):
        try:
            failure = json.loads((run / 'planner/failure.json').read_text())
            code = str(failure.get('code', ''))[:700]
            if code:
                label = ('模型服务连接失败' if code.startswith(('model_connect_', 'model_proxy_', 'model_transport_'))
                         else '模型响应等待失败' if code.startswith(('model_read_', 'model_write_', 'model_pool_'))
                         else '任务规划失败')
                return label + '：' + code
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            result=json.loads((run/'planner/result.json').read_text())
            declared=result.get('planner_declaration') or {}
            if declared.get('reason') == 'planner_no_progress_budget_exhausted':
                last = declared.get('last_failure') or {}
                return ('规划未取得进展，已结束重复推理：' + str(last.get('skill') or 'planning')
                        + '；' + str(last.get('reason') or last.get('error') or '连续查询未推进任务')[:700])
            failed=next((call for call in declared.get('primitive_calls',[]) if not call.get('ok')),None)
            if failed:
                skill=(failed.get('kwargs') or {}).get('name',failed.get('tool',''))
                return '任务未完成：'+str(skill)+'；'+str(failed.get('error','unknown'))[:700]
            if declared.get('exception'):
                return '任务未完成：'+str(declared['exception']).strip().splitlines()[-1][:700]
        except (OSError,ValueError,TypeError):
            pass
        return '任务异常结束，请查看技能与控制日志。'

    @classmethod
    def _planner_outcome(cls, run, returncode):
        # The CLI uses exit 2 for a measured non-success, including a deliberate
        # pending review. That is distinct from a crashed planner. Never let an
        # attention field hide a failed program, an error exit or failure.json.
        try:
            result=json.loads((run/'planner/result.json').read_text())
            declared=result.get('planner_declaration') or {}
            attention=result.get('attention')
            measured=result.get('measured') or {}
            if not attention and declared.get('status')=='stuck' and declared.get('summary'):
                attention=dict(skill='planning',reason=str(declared['summary'])[:700],
                               source='planner_declaration',motion_verified=False)
            failed=(declared.get('exception') or any(not call.get('ok')
                    for call in declared.get('primitive_calls',[])))
            if (returncode in (0,2) and measured.get('outcome')=='pending_review'
                    and isinstance(attention,dict) and attention.get('reason')
                    and not failed and not (run/'planner/failure.json').exists()):
                if attention['reason']=='planner_no_progress_budget_exhausted':
                    last=attention.get('last_failure') or {}
                    summary=('规划未取得进展，已结束重复推理：'+str(attention.get('skill',''))+'；'
                             +str(last.get('reason') or last.get('error') or '连续查询未推进任务')[:700])
                else:
                    summary='任务待处理：'+str(attention.get('skill',''))+'；'+str(attention['reason'])[:700]
                return 'review',summary,attention
        except (OSError,ValueError,TypeError,AttributeError):
            pass
        if returncode:
            return 'error',cls._planner_failure(run),None
        return 'review','完整程序已结束，物理结果按日志和视觉证据审核。',None

    def _stop_owner(self, processes, *, stop_request, store):
        status = processes.owner_call('status')
        self._check_owner_status(processes, status)
        request_id = None
        if (status.get('task_hash') and (status.get('stop_scope',{} ) or
                status.get('perception_context', {})).get('session_id')):
            request_id = uuid.uuid4().hex
            payload = self._binding(status, request_id,stopping=True)
            # A lost response is queried by ID; never replay a possibly consumed request.
            try:
                processes.owner_call('web_stop', **payload)
            except Exception as original:
                store.event('web_stop_rpc_error',title='停止请求回执异常',level='warn',
                            request_id=request_id,reason=str(original))
                try:processes.owner_call('web_control_status', request_id=request_id)
                except Exception as query:
                    raise RuntimeError(str(original)+'；结果查询：'+str(query)) from original
            store.event('web_stop_submitted', title='停止请求已提交', request_id=request_id)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if request_id:
                result = processes.owner_call('web_control_status', request_id=request_id)
                status = result['owner']
                self._check_owner_status(processes, status)
                stage = result['stage']
                if result['lifecycle'] == 'rejected':
                    raise RuntimeError('停止请求被控制端拒绝：' + result.get('reason', 'unknown'))
                if stage in ('measured_stationary', 'no_motion_authorized'):
                    store.event('web_stop_confirmed',title='控制端停止已确认',request_id=request_id,
                                stage=stage,hold_confirmation=status.get('hold_confirmation'))
                    if status.get('stop_scope'):
                        try:
                            processes.stop_planner()
                            processes.owner_call('web_close_session',**payload)
                        except Exception as exc:
                            # Cleanup transport failure cannot erase a received,
                            # bound physical hold receipt. Reaping is still owned
                            # by the supervisor's finally block.
                            store.event('web_cleanup_incomplete',title='停止已确认，清理回执异常',
                                        level='warn',reason=str(exc),stage=stage)
                    return stage
            else:
                status = processes.owner_call('status')
                self._check_owner_status(processes, status)
                if not any(status.get(k) for k in ('authorized', 'running', 'pending', 'hold_requested')):
                    return 'owner_hold_completed'
            time.sleep(.15)
        raise TimeoutError('未收到实测停稳确认。')

    def _wait_restart_stationary(self, processes, run, cancel, store):
        self._update(run,summary='正在重新准备：等待新控制端的实测停稳，尚未授权动作。')
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            if cancel.is_set():raise InterruptedError('重新准备期间已取消，未授权新任务。')
            processes.check()
            status=processes.owner_call('status')
            self._check_owner_status(processes,status)
            if any(status.get(k) for k in ('authorized','pending','running','hold_requested')):
                raise RuntimeError('重新准备时控制端状态异常，未授权新任务。')
            proof=status.get('restart_readiness') or {}
            if (proof.get('state')=='ready'
                    and proof.get('owner_instance_id')==processes.expected_owner_instance
                    and type(proof.get('measured_at_ns')) is int):
                store.event('workspace_restart_stationary',title='新控制端实测停稳，允许准备新任务',
                    recovery_of=self._metadata(run).get('recovery_of'),evidence=proof,
                    previous_stop_confirmation_unchanged=True)
                self._update(run,restart_requires_stationary=False,restart_evidence=proof)
                with self.lock:self.faulted=False;self.recovery_of=None
                return status
            if cancel.wait(.1):raise InterruptedError('重新准备期间已取消，未授权新任务。')
        raise TimeoutError('新控制端尚未确认实测停稳；未授权动作，可再次点击开始重新准备。')

    @contextmanager
    def _startup_stage(self, run, store, stage, title):
        started = time.monotonic_ns()
        self._update(run, summary=title)
        store.event('workspace_startup_stage_started', title=title, stage=stage)
        try:
            yield
        except BaseException:
            store.event('workspace_startup_stage_failed', title=title + '未完成', stage=stage,
                        elapsed_ms=(time.monotonic_ns() - started) / 1e6)
            raise
        else:
            store.event('workspace_startup_stage_completed', title=title + '完成', stage=stage,
                        elapsed_ms=(time.monotonic_ns() - started) / 1e6)

    def _execute(self, run, request, cancel):
        processes = None
        planner = None
        owner_started = False
        authorized = False
        stopped = False
        failure = None
        store = EvidenceStore(run)
        try:
            config = dict(self.config)
            route = config.get('strategy_routes', {}).get(request['config']['mode'])
            if route is not None:
                if set(route) != {'root', 'local_site', 'remote_bundle'}:
                    raise ValueError('invalid_frozen_strategy_route')
                config.update(route)
                # Numerical/boot workers are bound to their own immutable bundle.
                config.pop('_numerical_pool_socket', None)
            subscription = config.get('chatgpt_subscription_credentials', {}).get(request['config']['mode'])
            if subscription:
                config['_subscription_credentials'] = subscription
                config.pop('_model_pool_socket', None)
                socket_path = config.get('_subscription_model_pool_sockets', {}).get(request['config']['mode'])
                if socket_path:
                    config['_model_pool_socket'] = socket_path
            remote_site = '/home/user/Bruno/HARNESS/site/' + run.name
            task = freeze_site(config['local_site'], run / 'site', remote_site=remote_site,
                               request=request, run_name=run.name)
            processes = self.processes(config, run)
            if self.boot_client is not None and route is None:processes.boot_client=self.boot_client
            from .contracts import digest
            processes.expected_task_hash = digest(task)
            with self._startup_stage(run, store, 'site_copy', '正在准备本次任务配置'):
                processes.copy_site()
            with self._startup_stage(run, store, 'boot_recheck', '正在复核设备与标定'):
                receipt=processes.prepare_boot_recheck(cancel)
            store.event('boot_recheck_completed', title='设备与标定启动复核通过', receipt=receipt)
            def start_owner():
                nonlocal owner_started
                processes.start_owner()
                owner_started = True
                store.event('workspace_owner_initialization_started',title='控制端开始初始化（与辅助服务并行）')
            with self._startup_stage(run, store, 'auxiliary_services', '正在启动感知与规划服务'):
                concurrent=getattr(processes,'start_services_with_owner',None)
                if concurrent is None:processes.start_services(cancel)
                else:concurrent(cancel,start_owner)
            store.event('workspace_services_ready',title='辅助服务就绪')
            if cancel.is_set():
                raise InterruptedError('任务准备已取消。')
            with self._startup_stage(run, store, 'owner_ready', '正在初始化控制端与反馈'):
                if not owner_started:start_owner()
                initial = self._wait_status(processes, cancel)
            store.event('workspace_owner_ready',title='控制端反馈就绪')
            processes.expected_owner_instance = initial['owner_instance_id']
            with self._startup_stage(run, store, 'planner_prepare', '正在准备任务规划器'):
                planner = processes.start_planner(request)
                status = self._wait_status(processes, cancel, prepared=True, planner=planner)
            store.event('workspace_task_prepared',title='任务准备完成')
            if self._metadata(run).get('restart_requires_stationary'):
                status=self._wait_restart_stationary(processes,run,cancel,store)
            if cancel.is_set():
                raise InterruptedError('尚未授权，已取消。')
            request_id = uuid.uuid4().hex
            payload = self._binding(status, request_id)
            store.event('web_authorization_submitted', title='已提交本次运行授权', request_id=request_id)
            try:
                processes.owner_call('web_start', **payload)
            except Exception:
                processes.owner_call('web_control_status', request_id=request_id)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if cancel.is_set():
                    stage = self._stop_owner(processes, stop_request=True, store=store)
                    stopped = True
                    self._update(run, stop_confirmation=stage)
                    raise InterruptedError('已取消本次开始请求，控制端已确认。')
                outcome = processes.owner_call('web_control_status', request_id=request_id)
                self._check_owner_status(processes, outcome['owner'])
                if outcome['lifecycle'] == 'rejected':
                    raise RuntimeError('控制端未授权：' + outcome.get('reason', 'unknown'))
                if outcome['stage'] == 'owner_authorized':
                    if not outcome['owner'].get('authorized') or outcome['owner'].get('hold_requested'):
                        raise RuntimeError(self._authority_failure(outcome['owner']))
                    authorized = True
                    authorized_epoch=outcome['owner']['epoch']
                    break
                time.sleep(.1)
            else:
                raise TimeoutError('网页开始授权结果未知。')
            self._update(run, status='running', summary='完整任务执行中。')
            last_progress=None
            while planner.poll() is None and not cancel.wait(.2):
                processes.check()
                actual=processes.owner_call('status')
                self._check_owner_status(processes,actual)
                try:progress=json.loads((run/'planner/progress.json').read_text())
                except (OSError,ValueError):progress=None
                if isinstance(progress,dict) and progress!=last_progress:
                    last_progress=progress
                    phase=progress.get('state')
                    label={'waiting':'等待有效观测','needs_attention':'任务待处理',
                           'model':'Astra正在生成方案','program':'正在运行任务程序',
                           'planning':'正在规划','completed':'任务程序已结束',
                           'executing':'正在执行','ready':'观测就绪'}.get(phase,'完整任务执行中')
                    summary=label+('：'+str(progress['skill']) if progress.get('skill') else '')
                    if phase=='model' and progress.get('round'):summary+='（第'+str(progress['round'])+'轮）'
                    if progress.get('reason'):summary+='；'+str(progress['reason'])[:500]
                    self._update(run,summary=summary)
                    if phase=='waiting':store.event('workspace_waiting',title='等待有效观测',
                        level='warn' if progress.get('warning') else 'info',reason=summary,progress=progress)
                revoked=actual.get('authority_revocation') or {}
                if (not actual.get('authorized') or actual.get('epoch')!=authorized_epoch):
                    # finish_task revokes authority before draining evidence.
                    # Fault revocation must end the UI run even if its planner
                    # missed a brief authorization between two status reads.
                    if revoked.get('reason')!='task_finished':
                        raise RuntimeError(self._authority_failure(actual))
            planner_returncode=planner.poll()
            stage = self._stop_owner(processes, stop_request=cancel.is_set(), store=store)
            stopped = True
            self._update(run, stop_confirmation=stage)
            if cancel.is_set():
                processes.stop_planner()
                final, summary = 'cancelled', '任务已取消，控制端保持已确认。'
            else:
                final,summary,attention=self._planner_outcome(run,planner_returncode)
                if final=='error':
                    store.event('workspace_task_failed',title='任务未完成',level='error',reason=summary)
                elif attention:
                    store.event('workspace_task_needs_attention',title='任务待处理',level='warn',
                                reason=summary,attention=attention)
            self._update(run, status=final, summary=summary)
        except BaseException as exc:
            failure = str(exc)[:1000]
            store.event('workspace_failure', title='本次运行未完成', level='error', reason=failure)
            self._update(run,status='stopping' if owner_started and not stopped else 'error',
                         summary=failure)
            if processes and owner_started and not stopped:
                try:
                    stage = self._stop_owner(processes, stop_request=True, store=store)
                    stopped = True
                    self._update(run, stop_confirmation=stage)
                except Exception as stop_error:
                    store.event('web_stop_unconfirmed', title='停止反馈未确认', level='error', reason=str(stop_error))
            if owner_started and not stopped:
                with self.lock:
                    self.faulted = True
                    self.recovery_of = self.run_id(run.name)
                self._update(run,restart_requires_stationary=True)
            self._update(run, status='cancelled' if isinstance(exc, InterruptedError) and (stopped or not authorized)
                         else 'error', summary=failure)
        finally:
            if processes:
                processes.close()
            self._update(run, ended_at=time.time())
            # Collection remains part of this run; never copy another owner's "latest" data.
            # Failure evidence is most important when stop feedback was lost.
            # The collector independently requires the bound task identity and
            # a quiescent source under the existing shared control lock. Neither
            # process exit nor successful collection proves physical stopping.
            if processes and planner and (run / 'planner/trace.jsonl').is_file() and not self.closing:
                try:
                    if hasattr(processes, 'seal_collection'):
                        receipt = processes.seal_collection()
                        store.write_json('export-seal.json', receipt)
                        from .export_worker import enqueue
                        enqueue(run, self.config, receipt, stop_confirmed=stopped)
                        store.event('workspace_export_queued', title='机器人原件已封存，后台导出中；可开始下一任务')
                    else:
                        result = processes.collect()
                        result['stop_feedback_confirmed'] = stopped
                        store.write_json('collection-result.json', result)
                        store.event('workspace_evidence_collected', title='本次原始证据已收集',
                                    stop_feedback_confirmed=stopped)
                except Exception as exc:
                    store.event('workspace_collection_incomplete', title='结果封存未完成', level='warn', reason=str(exc))

    def begin_shutdown(self):
        with self.lock:
            self.closing = True
            self.cancel.set()

    def runs(self):
        found = []
        for run in sorted(self.runs_root.glob('web-*'), reverse=True):
            if run.is_symlink() or not re.fullmatch(r'web-[a-f0-9]{32}', run.name):
                continue
            try:
                found.append(self._render(run))
            except (OSError, ValueError):
                continue
        return dict(runs=sorted(found, key=lambda r:r['started_at'], reverse=True)[:100])

    def run(self, run_id):
        if not re.fullmatch(r'harness-[a-f0-9]{20}', run_id):
            raise ValueError('运行身份无效。')
        for run in self.runs_root.glob('web-*'):
            if not run.is_symlink() and self.run_id(run.name) == run_id:
                return self._render(run, detail=True)
        raise ValueError('运行记录不存在。')

    def llm(self, run_id, request_id=None, image_id=None):
        from .llm_trace import exchanges
        if not re.fullmatch(r"harness-[a-f0-9]{20}", run_id): raise ValueError("运行身份无效。")
        for run in self.runs_root.glob("web-*"):
            if not run.is_symlink() and self.run_id(run.name)==run_id:
                return exchanges(run/"planner",started_ns=self._metadata(run)["started_ns"],request_id=request_id,image_id=image_id)
        raise ValueError("运行记录不存在。")

    def logs(self, run_id):
        return '\n'.join(json.dumps(event, ensure_ascii=False) for event in self.run(run_id)['events']) + '\n'

    def run_delta(self, run_id, cursor=None):
        return dict(schema=self.records.DELTA_SCHEMA,
                    **self.reader.event_delta(self.run(run_id),cursor,cursor_prefix='u1:'))


def workspace_server(supervisor, *, port=19843):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, *_):
            pass

        def send_value(self, status, value, *, text=False):
            raw = (value if text else json.dumps(value, ensure_ascii=False, allow_nan=False)).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/x-ndjson' if text else 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            try:
                parsed=urlsplit(self.path)
                if parsed.query and re.fullmatch(r'/runs/harness-[a-f0-9]{20}',parsed.path):
                    if len(self.path)>512:raise ValueError('任务游标过长。')
                    query=parse_qs(parsed.query,keep_blank_values=True,max_num_fields=1)
                    if set(query)!={'cursor'}:raise ValueError('未知任务读取参数。')
                    value=supervisor.run_delta(parsed.path.split('/')[2],query['cursor'][0])
                elif self.path == '/state':
                    value = supervisor.state()
                elif self.path == '/runs':
                    value = supervisor.runs()
                elif re.fullmatch(r'/runs/harness-[a-f0-9]{20}/llm(?:/[a-f0-9]{32}(?:/[a-f0-9]{64})?)?', self.path):
                    parts=self.path.split('/')
                    value=supervisor.llm(parts[2],parts[4] if len(parts)>4 else None,parts[5] if len(parts)>5 else None)
                elif re.fullmatch(r'/runs/harness-[a-f0-9]{20}/logs', self.path):
                    self.send_value(200, supervisor.logs(self.path.split('/')[2]), text=True)
                    return
                elif re.fullmatch(r'/runs/harness-[a-f0-9]{20}', self.path):
                    value = supervisor.run(self.path.split('/')[2])
                else:
                    raise ValueError('未知任务读取接口。')
                self.send_value(200, value)
            except (ValueError, OSError) as exc:
                self.send_value(404, dict(error=str(exc)))

        def do_POST(self):
            if (self.headers.get('Origin') or self.headers.get('Content-Type') != 'application/json'
                    or self.headers.get('X-F1-Harness') != 'workspace'
                    or self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port)):
                self.send_value(403, dict(error='仅接受本机infra代理的明确操作。'))
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('请求长度无效。')
                body = json.loads(self.rfile.read(length))
                if self.path == '/start':
                    value = supervisor.start(body)
                elif self.path == '/stop':
                    value = supervisor.stop(body)
                else:
                    raise ValueError('未知任务操作接口。')
                self.send_value(200, value)
            except (ValueError, OSError) as exc:
                self.send_value(409, dict(error=str(exc)))

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)
