"""Host adapters to the loopback RPC facade. An unknown submission is queried, never replayed."""
import time
import uuid
from pathlib import Path

from rpent.utils.rpc.http_rpc import HttpRpcClient
from rpent.utils.rpc.rpc_client import RpcError
from .contracts import Mode, Observation, Task, SkillIntent, SkillResult, digest, HarnessError
from .gateway import OWNER_METHODS
from .ipc import UnixClient, GEOMETRY_HTTP_TIMEOUT_S, GEOMETRY_OWNER_TIMEOUT_S
from .engine import REPLANNABLE_SKILLS
from .observation import OBSERVATION_WINDOW_S, measured_alignment_pair


OBSERVATION_READINESS_S = 15.


class ObservationWaitEnded(HarnessError):
    """No action was submitted; the task can finish with an explicit review reason."""

    def __init__(self, reason, *, scope=None):
        super().__init__(reason)
        # Scope is captured during the read loop, not reconstructed from a
        # later status that might belong to a replacement task or START.
        self.scope = scope


class _AcquisitionDeadline(TimeoutError):
    """Local read budget exhausted before I/O, not a failed network request."""


def _observation_transport_failure(exc):
    if isinstance(exc, _AcquisitionDeadline):return False
    return (isinstance(exc, OSError) or isinstance(exc, RpcError) and
            str(exc).startswith((exc.method+': HTTP request failed:',
                                 exc.method+': invalid JSON response:')))


def _socket_timeout(exc):
    """Inspect transport causes, not arbitrary observer error text."""
    seen=set()
    while isinstance(exc,BaseException) and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc,TimeoutError):return True
        exc=exc.__cause__ or getattr(exc,'reason',None)
    return False


def _needs_visual_prompt(exc):
    if isinstance(exc,RpcError):
        if exc.method not in ('observe','observe_with_status'):return False
        code=str(exc).removeprefix(exc.method+': ')
    elif isinstance(exc,ValueError):code=str(exc)
    else:return False
    if code.startswith(('mask_view=head:','mask_view=right:')):code=code.split(':',1)[1]
    return (code=='object_not_visible_in_either_calibrated_view'
            or code.startswith('object_identity_unresolved')
            or code=='mask_uncertain_requires_new_prompt'
            or code.startswith('mask_uncertain_requires_new_prompt:'))


class OwnerProxy:
    def __init__(self,path:Path):
        self.client=UnixClient(path,timeout=2,scene_evidence=True)
        self.geometry_client=UnixClient(path,timeout=GEOMETRY_OWNER_TIMEOUT_S,scene_evidence=True)
        self.preview_client=UnixClient(path,timeout=32,scene_evidence=True)
        self.session=None

    def __getattr__(self,name):
        if name not in OWNER_METHODS:raise AttributeError(name)
        def call(**kwargs):
            client={'preview_skill':self.preview_client,'geometry_context':self.geometry_client}.get(name,self.client)
            value=client.call(name,**kwargs)
            if name=='open_session':self.session=value['session_id']
            if name=='close_session':self.session=None
            return value
        return call

    @property
    def mode(self):return Mode(self.status()['mode'])

    @property
    def hardware_ready(self):return self.status()['hardware_ready']

    def update_observation(self,observation):
        from .scene_evidence import observation_wire
        return self.client.call('ingest_observation',observation=observation_wire(observation))

    def tick(self):pass  # Heartbeat/deadline enforcement belongs to the owner tick.

    def revoke(self,reason):
        if self.session:
            try:self.close_session(session_id=self.session)
            finally:self.session=None


class RemoteTaskBackend:
    def __init__(self,url,task:Task):
        from urllib.parse import urlparse
        if urlparse(url).hostname not in ('localhost','127.0.0.1'):
            raise ValueError('use an explicit loopback SSH tunnel')
        self.rpc=HttpRpcClient(url,enable_sessions=False)
        self.task=task
        session=self.call('open_session')
        self.session,self.instance=session['session_id'],session['owner_instance_id']
        self.call('prepare_task',session_id=self.session,task=task.model_dump())
        self.steps,self._solved=0,False
        self.pending=None
        self.deferred_grasp=None
        self._closed=False

    def call(self,method,*,timeout_s=2,**kwargs):
        value=self.rpc.call(method,kwargs=kwargs,timeout_s=timeout_s)
        if method=='finish_task':
            # Only a received owner reply may suppress a duplicate cleanup RPC.
            self._finished_task_receipt=(kwargs.get('session_id'),value)
        elif method in ('open_session','prepare_task','close_session'):
            self._finished_task_receipt=None
        return value

    def heartbeat(self):return self.call('heartbeat',session_id=self.session)

    def _progress(self,state,**detail):
        # Planner-side UI feedback; never runs in the real-time owner loop.
        import json,tempfile
        store=getattr(self,'evidence_store',None)
        root=getattr(store,'root',None)
        if root is None:return
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(mode='w',dir=root,prefix='.progress-',delete=False) as stream:
                temporary=Path(stream.name)
                stream.write(json.dumps(dict(state=state,**detail),ensure_ascii=False)+'\n')
            temporary.replace(Path(root)/'progress.json')
        except OSError:
            # A display failure must never replace an authoritative action
            # receipt or change whether a published request gets replayed.
            if temporary is not None:
                try:temporary.unlink(missing_ok=True)
                except OSError:pass

    def _needs_attention(self,skill,reason,verification_request_id=None,*,resume_request_id=None):
        self.attention=dict(skill=skill,reason=reason,submitted=False)
        checkpoint=verification_request_id or resume_request_id
        if checkpoint is not None:
            # The continuation was never submitted. Only the original owner
            # receipt can describe whether the preceding operation moved.
            original=self.call('skill_result',session_id=self.session,request_id=checkpoint)
            field='verification_request_id' if verification_request_id else 'resume_request_id'
            if original.get('request_id')!=checkpoint:
                raise HarnessError('continuation_original_request_mismatch')
            if original.get(field)!=checkpoint:
                # A caller may refer to a completed request with no eligible
                # checkpoint. Preserve its publication state and stop here.
                original=dict(request_id=checkpoint,outcome='pending_review',lifecycle='rejected',
                    reason='continuation_original_checkpoint_unavailable',no_motion=original.get('no_motion'))
            self.attention.update(request_id=checkpoint,no_motion=original.get('no_motion'),
                continuation_submitted=False,observation_retry_reason=reason)
        self._progress('needs_attention',**self.attention)
        store=getattr(self,'evidence_store',None)
        if store is not None:store.event('task_needs_attention',**self.attention)
        if checkpoint is not None:
            return dict(original,observation_retry_reason=reason,continuation_submitted=False)
        return SkillResult(request_id=uuid.uuid4().hex,lifecycle='rejected',
            outcome='pending_review',reason=reason,no_motion=True).model_dump()

    def _await_observation(self, *, timeout_s=OBSERVATION_WINDOW_S, expected_scope=None, skill=None, verification=False,
                           prompt_handoff=False,viewpoint_handoff=False,cancelled=lambda:False,
                           reobserved_alignment=None):
        """Retry only read-only acquisition before a request ID or action exists.

        The runner keeps the existing heartbeat alive. Owner monotonic time is
        used for freshness; the planner host's monotonic clock is a different domain.
        """
        from .observation import require_geometry
        started=time.monotonic();deadline=started+timeout_s
        initial_scope=expected_scope;attempts=0;last_reason='observation_missing';logged_reason=None
        transport_error=None;first_reason=None;warned=False

        def event(kind,**values):
            store=getattr(self,'evidence_store',None)
            if store is not None:store.event(kind,**values)

        def read(method):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise _AcquisitionDeadline('observation_readiness_deadline')
            try:return self.call(method,timeout_s=min(2.,remaining))
            except Exception as exc:
                # A socket deadline shortened to the remaining acquisition budget
                # cannot establish a network outage. Keep the last measured
                # perception cause. Full transport deadlines and reset/invalid
                # replies remain fatal; no action submission uses this helper.
                if remaining<2. and time.monotonic()>=deadline and _socket_timeout(exc):
                    raise _AcquisitionDeadline('observation_readiness_deadline') from exc
                raise

        def scope(status):
            if cancelled():raise HarnessError('observation_wait_cancelled')
            context=status.get('perception_context') or {}
            if (status.get('owner_instance_id')!=self.instance or status.get('task_hash')!=digest(self.task)
                    or context.get('session_id')!=self.session or not status.get('authorized')
                    or status.get('hold_requested')):
                raise HarnessError('observation_wait_authority_changed')
            return (self.instance,self.session,status['task_hash'],status['epoch'])

        while time.monotonic()<deadline:
            if cancelled():raise HarnessError('observation_wait_cancelled')
            attempts+=1
            snapshot=None;snapshot_error=None
            if reobserved_alignment is not None:
                # Reobserve made no publication. Its admitted measurement is
                # still the owner's request snapshot; live perception advances
                # independently and must revalidate the resulting candidate.
                source, source_intent, generation = reobserved_alignment
                reobserved_alignment = None
                status = read('status')
                if (scope(status) != (source_intent.owner_instance_id, source_intent.session_id,
                                     source_intent.task_hash, source_intent.epoch)
                        or status.get('perception_context', {}).get('task_generation') != generation):
                    raise HarnessError('observation_wait_epoch_changed')
                if (status.get('alignment_snapshot_id') == source.observation_id
                        and status.get('phase') == 'reobserve' and not status.get('motion_wait')
                        and not status.get('running') and not status.get('pending')):
                    snapshot = dict(observation=source, status=status)
                    event('reobserved_alignment_input', observation_id=source.observation_id,
                          reobserve_request_id=source_intent.request_id, epoch=source_intent.epoch)
            # The advertised combined read already carries the owner's status
            # after ingestion. Avoid a redundant network round trip before it.
            # A failed read still needs status to distinguish camera waiting
            # from revoked authority or an operator-attention condition.
            if snapshot is None and getattr(self,'observation_snapshot_supported',False):
                try:snapshot=read('observe_with_status')
                except _AcquisitionDeadline:break
                except Exception as exc:
                    if _observation_transport_failure(exc):
                        transport_error=type(exc).__name__+':'+str(exc)[:400]
                        last_reason='observation_transport_unavailable:'+transport_error
                        break
                    snapshot_error=exc
            try:before=snapshot['status'] if snapshot is not None else read('status')
            except _AcquisitionDeadline:break
            except Exception as exc:
                transport_error=type(exc).__name__+':'+str(exc)[:400]
                if last_reason=='observation_missing':last_reason='owner_status_unavailable'
                break
            current_scope=scope(before)
            if initial_scope is None:initial_scope=current_scope
            elif current_scope!=initial_scope:raise HarnessError('observation_wait_epoch_changed')
            condition=before.get('observation_readiness') or {}
            if condition.get('state')=='needs_attention':
                event('observation_needs_attention',condition=condition,submitted=False)
                raise ObservationWaitEnded('observation_needs_attention:'+str(condition.get('code')),
                                           scope=initial_scope)
            if not warned and time.monotonic()-started>=30:
                warned=True
                self._progress('waiting',skill=skill,reason=last_reason,first_reason=first_reason,
                               waited_ms=(time.monotonic()-started)*1000,warning=True)
                event('observation_wait_warning',reason=last_reason,first_reason=first_reason,
                      condition=condition,waited_ms=(time.monotonic()-started)*1000,
                      submitted=False,continues_within_task_budget=True)
            if before.get('motion_wait'):
                last_reason='owner_motion_wait'
                if last_reason!=logged_reason:
                    self._progress('waiting',skill=skill,reason=last_reason)
                    event('waiting_for_stable_observation',reason=last_reason,attempts=attempts)
                    logged_reason=last_reason
                time.sleep(min(.1,max(0.,deadline-time.monotonic())))
                continue
            try:
                if snapshot_error is not None:raise snapshot_error
                payload=snapshot['observation'] if snapshot is not None else read('observe')
            except _AcquisitionDeadline:break
            except Exception as exc:
                # The observer may still be rebuilding its stationary window.
                # Never classify a failed submit or result query as an observation retry.
                if _observation_transport_failure(exc):
                    transport_error=type(exc).__name__+':'+str(exc)[:400]
                    last_reason='observation_transport_unavailable:'+transport_error
                    break
                last_reason='observation_unavailable:'+type(exc).__name__+':'+str(exc)[:400]
                if prompt_handoff and _needs_visual_prompt(exc):
                    # The authorized bootstrap needs Astra to seed the target.
                    # Repeating the same unseeded read cannot produce geometry.
                    # The caller rechecks authority before exposing images; this
                    # is not an observation or permission to submit a motion.
                    event('observation_prompt_required',reason=last_reason,attempts=attempts,
                          waited_ms=(time.monotonic()-started)*1000,submitted=False)
                    raise ObservationWaitEnded('observation_prompt_required:'+last_reason,
                                               scope=initial_scope) from exc
            else:
                observation=Observation.model_validate(payload)
                try:after=snapshot['status'] if snapshot is not None else read('status')
                except _AcquisitionDeadline:break
                except Exception as exc:
                    transport_error=type(exc).__name__+':'+str(exc)[:400]
                    last_reason='owner_status_unavailable_after_observation';break
                if scope(after)!=initial_scope:raise HarnessError('observation_wait_epoch_changed')
                owner_now=after.get('owner_monotonic_ns');max_age=after.get('observation_age_ns')
                if (type(owner_now) is not int or owner_now<0 or type(max_age) is not int
                        or not 0<max_age<=500_000_000):
                    raise HarnessError('owner_freshness_clock_contract_missing')
                try:
                    if after.get('motion_wait'):raise ValueError('owner_motion_wait')
                    after_ns=after.get('observation_after_ns',0)
                    if after_ns and (observation.received_ns<=after_ns or observation.state_received_ns is None
                            or observation.state_received_ns<=after_ns
                            or any(frame.received_ns<=after_ns for frame in observation.frames)):
                        raise ValueError('observation_before_motion_wait_recovery')
                    if observation.calibration_id!=self.task.calibration_id:
                        raise ValueError('observation_calibration_mismatch')
                    if after.get('mode')=='fake':
                        if (not observation.synthetic or observation.faults or not observation.stationary
                                or not 0<=owner_now-observation.received_ns<=max_age):
                            raise ValueError('fake_observation_not_ready')
                    else:
                        identity=after.get('recording_identity') or {}
                        if not identity.get('host_boot_id') or observation.host_boot_id!=identity['host_boot_id']:
                            raise ValueError('observation_owner_boot_mismatch')
                        # An explicit observation after placement may inspect the fixture-only
                        # scene. This does not authorize reobserve/grasp as an action.
                        geometry_skill=('withdraw_reset' if skill=='reobserve'
                            and 'withdraw_reset' in self.task.allowed_skills
                            and (after.get('perception_context') or {}).get('placement_verified') is True else skill)
                        require_geometry(observation,now_ns=owner_now,max_age_ns=max_age,goal=self.task.goal,
                                         admission_profile=self.task.admission_profile,skill=geometry_skill,
                                         verification=verification,read_only_reference=True,hole_id=self.task.hole_id)
                    if skill=='transfer_above_hole' and not verification:
                        target=next((obj for obj in observation.objects if obj.object_id==self.task.object_id),None)
                        if target is not None and target.held is True and target.eef_object_pose is None:
                            # CAN can confirm grasp before RGB-D localizes the
                            # attachment required for the swept-volume check.
                            raise ValueError('held_object_attachment_transform_pending')
                        completed=after.get('completed_skills') or ()
                        if ('verify_grasp' in completed and not {'release','release_supported'}.intersection(completed)
                                and (target is None or target.held is None)):
                            raise ValueError('verified_grasp_current_attachment_evidence_pending')
                    if time.monotonic()>=deadline:
                        last_reason='observation_readiness_deadline';break
                except ValueError as exc:
                    last_reason=str(exc)
                    missing_held_aperture=(skill=='align' and last_reason.startswith('aperture_measurement_required:')
                        and any(obj.object_id==self.task.object_id and obj.visible and obj.held is True
                                and obj.eef_object_pose is not None for obj in observation.objects))
                    if viewpoint_handoff and (missing_held_aperture or (skill=='approach_grasp'
                            and last_reason=='local_environment_only_supports_world_observation_motion')):
                        # A valid observation can change the next action before
                        # the readiness window expires. The recipient still
                        # has to plan any view move against the current scene.
                        try:
                            require_geometry(observation,now_ns=owner_now,max_age_ns=max_age,goal=self.task.goal,
                                admission_profile=self.task.admission_profile,skill='reobserve',
                                read_only_reference=True,hole_id=self.task.hole_id)
                        except ValueError:
                            pass
                        else:
                            event('observation_viewpoint_required',reason=last_reason,attempts=attempts,
                                observation_id=observation.observation_id,submitted=False)
                            raise ObservationWaitEnded('observation_viewpoint_required:'+last_reason,
                                scope=initial_scope) from exc
                    if (last_reason=='coarse_geometry_cannot_authorize_precision_task'
                            and skill=='approach_grasp' and self.task.admission_profile=='development_coarse'
                            and observation.geometry_scope=='coarse_object_verification'
                            and any(o.object_id==self.task.object_id and o.visible and o.pose is not None
                                    for o in observation.objects)):
                        last_reason='grasp_collision_geometry_missing'
                else:
                    deferred=getattr(self,'deferred_grasp',None)
                    if (deferred and observation.geometry_scope in ('precision','coarse_static_task')
                            and any(o.object_id==self.task.object_id and o.visible and o.pose is not None
                                    and o.held is not True and o.eef_object_pose is None for o in observation.objects)
                            and (observation.fixture is not None or any(o.object_id==self.task.hole_id
                                    and o.pose is not None for o in observation.objects))):
                        deferred['ready']=True
                    self._progress('ready',skill=skill,observation_id=observation.observation_id)
                    event('observation_ready_before_submit',attempts=attempts,
                          waited_ms=(time.monotonic()-started)*1000,
                          observation_id=observation.observation_id,epoch=after['epoch'])
                    return observation,after
            if last_reason!=logged_reason:
                if first_reason is None:first_reason=last_reason
                self._progress('waiting',skill=skill,reason=last_reason,first_reason=first_reason)
                event('waiting_for_stable_observation',reason=last_reason,attempts=attempts)
                logged_reason=last_reason
            time.sleep(min(.1,max(0.,deadline-time.monotonic())))
        if cancelled():raise HarnessError('observation_wait_cancelled')
        event('observation_readiness_timeout',reason=last_reason,attempts=attempts,
              waited_ms=(time.monotonic()-started)*1000,submitted=False,
              transport_error=transport_error,first_reason=first_reason)
        if transport_error:
            raise HarnessError('observation_readiness_timeout:'+last_reason)
        raise ObservationWaitEnded('observation_readiness_timeout:'+last_reason,scope=initial_scope)

    def _missing_observation(self,exc):
        """A normal unavailable measurement is data; changed authority is not.

        Shared by observe and preview so one missing target cannot truncate a
        Python cell before its explicit image request or alternative strategy.
        """
        from .conditions import observation_condition,with_observation_details
        reason=str(exc).removeprefix('observation_readiness_timeout:').removeprefix('observation_prompt_required:')
        if reason.startswith('observation_unavailable:'):
            reason=reason.removeprefix('observation_unavailable:')
            for wrapper in ('RpcError:observe_with_status: ', 'RpcError:observe: ', 'ValueError:'):
                if reason.startswith(wrapper):
                    reason=reason.removeprefix(wrapper);break
        condition=observation_condition(reason)
        if (condition['state']!='waiting' or reason.startswith((
                'owner_', 'FileNotFoundError', 'feedback_expired')) or exc.scope is None):
            raise exc
        status=self.call('status')
        context=status.get('perception_context') or {}
        scope=(status.get('owner_instance_id'),context.get('session_id'),status.get('task_hash'),status.get('epoch'))
        if scope!=exc.scope or not status.get('authorized') or status.get('hold_requested'):
            raise HarnessError('observation_wait_authority_changed') from exc
        latest=status.get('observation_readiness') or {}
        if latest and (latest.get('state')=='needs_attention'
                       or observation_condition(latest.get('code'))['state']=='needs_attention'):
            raise HarnessError('observation_wait_identity_changed') from exc
        diagnostic=dict(code='observation_unavailable',error_type=type(exc).__name__,
            metrics=dict(reason=str(exc),recovery_hint='refresh_scene'))
        condition=with_observation_details(condition,status)
        return dict(planned=False,no_motion=True,executable=False,current_scope=True,
            reason=str(exc),planning_diagnostic=diagnostic,condition=condition,
            geometry_available=False,geometry_status='unavailable',available_skills=[],cartesian_motion=False,
            search_entities=latest.get('search_entities',{}),motion_authorized=False,
            motion_evidence=False,recovery_hint='refresh_scene'),status

    def read_program_target_state(self):
        return self.execute_tool('geometry_context',{},_target_presence=True)

    def execute_tool(self,name,input_dict,*,prompt_handoff=False,_target_presence=False):
        # Host provenance for this public call, never model-supplied metadata.
        # Internal zero-publication retries must remain attributable to the
        # enclosing program even though only the final receipt is returned.
        self.last_request_chain=[]
        alignment = getattr(self, '_reobserved_alignment', None)
        self._reobserved_alignment = None
        if name=='aperture_observation' and not input_dict:
            return self.call('aperture_observation')
        if name=='task_context':
            if input_dict:raise HarnessError('unexpected_task_context_arguments')
            return dict(instruction=self.task.operator_instruction,task_id=self.task.task_id,
                        task_hash=digest(self.task),goal=self.task.goal)
        if name in ('locate_target','locate_entities'):
            expected={'camera','box_normalized'} if name=='locate_target' else {'camera','entities'}
            if set(input_dict)!=expected:raise HarnessError('invalid_target_hint_arguments')
            if self.pending:raise HarnessError('unresolved_submission_requires_query')
            camera=input_dict['camera']
            meta=getattr(self,'last_viewed_images',{}).get(camera)
            if meta is None:raise HarnessError('view_camera_before_target_hint')
            from .target_relocalization import source_search_box
            if name=='locate_target':hint=source_search_box(meta,input_dict['box_normalized'])
            else:
                entities=input_dict['entities']
                if not isinstance(entities,list) or not 1<=len(entities)<=8:raise HarnessError('invalid_entity_hints')
                hints=[];seen=set()
                for entity in entities:
                    if (not isinstance(entity,dict) or set(entity)-{'object_id','box_normalized','points_normalized','point_labels'}
                            or not isinstance(entity.get('object_id'),str) or not 0<len(entity['object_id'])<=128
                            or entity['object_id'] in seen):raise HarnessError('invalid_entity_hints')
                    seen.add(entity['object_id'])
                    value=source_search_box(meta,entity.get('box_normalized'),
                        points_normalized=entity.get('points_normalized'),point_labels=entity.get('point_labels'))
                    hints.append(dict(value,object_id=entity['object_id']))
                hint=dict(hints[0],entity_hints=hints)
            status=self.call('status')
            if not status.get('authorized') or status.get('hold_requested'):raise HarnessError('target_hint_not_authorized')
            receipt=self.call('locate_target',session_id=self.session,owner_instance_id=self.instance,
                task_hash=digest(self.task),epoch=status['epoch'],hint=hint)
            if (getattr(self,'deferred_grasp',None) and receipt.get('accepted') is True
                    and receipt.get('search_only') is True):
                self.deferred_grasp.update(relocalized=True,ready=False)
            return receipt
        if name in ('geometry_context','preview_skill','inspect_plan'):
            if self.pending:raise HarnessError('unresolved_submission_requires_query')
            if name=='geometry_context' and input_dict:raise HarnessError('unexpected_geometry_arguments')
            if name!='geometry_context' and set(input_dict)-{'name','parameters'}:
                raise HarnessError('unexpected_preview_arguments')
            remaining=OBSERVATION_WINDOW_S
            if getattr(self,'task_deadline',None) is not None:
                remaining=min(remaining,self.task_deadline-time.monotonic())
            if remaining<=0:raise ObservationWaitEnded('task_budget_exhausted_before_observation')
            try:
                observation,status=self._await_observation(timeout_s=remaining,skill='reobserve')
            except ObservationWaitEnded as exc:
                preview,status=self._missing_observation(exc)
                store=getattr(self,'evidence_store',None)
                if store is not None:store.event('geometric_preview_observation_unavailable',
                    tool=name,skill=input_dict.get('name'),scope=exc.scope,**preview)
                if name=='geometry_context':return preview
                if name=='inspect_plan':return dict(preview,preview=preview,geometry={key:preview[key] for key in
                    ('geometry_status','available_skills','cartesian_motion','search_entities','motion_authorized')})
                return preview
            catalog=(status.get('observation_readiness') or {}).get('search_entities',{})
            if name=='geometry_context':
                if _target_presence and status.get('mode')!='fake' and observation.geometry_scope!='coarse_fixture_return':
                    # _await_observation binds current authority, epoch, calibration,
                    # camera evidence and owner freshness. The ensuing skill acquires
                    # its own admissible geometry and goes through normal planning.
                    target=next((o for o in observation.objects if o.object_id==self.task.object_id),None)
                    return dict(object_pose=list(target.pose) if target and target.pose is not None else None,
                        object_id=self.task.object_id,world_frame=target.frame_id if target else None,
                        observation_id=observation.observation_id,calibration_id=observation.calibration_id,
                        measurement_received_ns=observation.received_ns,motion_authorized=False)
                from .planner_geometry import pack_geometry
                return dict(pack_geometry(self.call(name,session_id=self.session,timeout_s=GEOMETRY_HTTP_TIMEOUT_S)),
                            search_entities=catalog)
            # Leave transport margin inside the existing preview RPC window.
            # Every stage consumes this same remaining planning budget.
            planning_seconds=25. if self.task.goal=='placed' else 10.
            if getattr(self,'task_deadline',None) is not None:
                planning_seconds=min(planning_seconds,self.task_deadline-time.monotonic())
            if planning_seconds<=0:raise ObservationWaitEnded('task_budget_exhausted_before_planning')
            intent=SkillIntent(request_id=uuid.uuid4().hex,session_id=self.session,owner_instance_id=self.instance,
                task_id=self.task.task_id,task_hash=digest(self.task),epoch=status['epoch'],
                observation_id=observation.observation_id,scene_version=observation.scene_version,
                calibration_id=self.task.calibration_id,skill=input_dict['name'],
                budget_ms=max(1,int(planning_seconds*1000)),parameters=input_dict.get('parameters',{}))
            self._progress('planning',skill=intent.skill,preview=True)
            result=self.call('preview_skill',intent=intent.model_dump(),timeout_s=30,
                             **({'include_geometry':True} if name=='inspect_plan' else {}))
            if name=='inspect_plan' and isinstance(result.get('geometry'),dict):
                result['geometry']['search_entities']=catalog
            self._progress('ready' if result.get('planned') else 'waiting',skill=intent.skill,
                           reason=result.get('reason'),preview=True)
            if intent.skill == 'align' and not result.get('planned'):
                preview = result.get('preview', result)
                diagnostics = preview.get('planning_diagnostic') or {}
                missing = diagnostics.get('metrics', {}).get('missing_entities') or preview.get('missing_entities', [])
                if any(row.get('role') == 'aperture' for row in missing if isinstance(row, dict)):
                    try:
                        result['left_aperture_observation'] = self.call('aperture_observation', timeout_s=1)
                    except (OSError, RpcError) as exc:
                        result['left_aperture_observation'] = dict(camera='left', available=False,
                            reason=type(exc).__name__, motion_evidence=False)
            return result
        if name=='observe':
            # Return unavailable evidence to the caller within the same short
            # acquisition budget used before skills. This does not shorten an
            # accepted motion or create a fresh task/authorization deadline.
            deadline=getattr(self,'task_deadline',None)
            if not self.pending and not self._solved:
                remaining=OBSERVATION_WINDOW_S if deadline is None else min(OBSERVATION_WINDOW_S,deadline-time.monotonic())
                if remaining<=0:
                    self._needs_attention('observe','task_budget_exhausted_before_observation')
                    raise ObservationWaitEnded('task_budget_exhausted_before_observation')
                try:
                    observation,status=self._await_observation(timeout_s=remaining,skill='reobserve',
                                                               prompt_handoff=True)
                except ObservationWaitEnded as exc:
                    if prompt_handoff:raise
                    missing,status=self._missing_observation(exc)
                    self._progress('waiting',skill='observe',reason=missing['reason'])
                    return dict(missing,observation=None,status=status,
                                task_instruction=self.task.operator_instruction)
                from .local_environment import local_scope
                partial=local_scope(observation)
                result=dict(observation=observation.model_dump(
                    exclude={'local_collision_evidence','local_collision_evidence_ref',
                             'measured_gripper','measured_gripper_ref'}),status=status,
                    task_instruction=self.task.operator_instruction)
                if observation.measured_gripper is not None:
                    from .gripper_contact import measurement_summary
                    result['gripper_feedback']=measurement_summary(observation.measured_gripper,
                        min((f.received_ns for f in observation.frames),default=observation.received_ns))
                if partial and _target_presence:
                    result['local_environment_available']=True
                elif partial:
                    # The owner supplies current TCP and a bounded occupancy
                    # summary. Depth grids remain on the numerical side, even
                    # when a Python cell prints the entire observe() result.
                    from .planner_geometry import pack_geometry
                    geometry=self.call('geometry_context',session_id=self.session,timeout_s=GEOMETRY_HTTP_TIMEOUT_S)
                    # observe() ingests a fixed owner snapshot; live updates use
                    # a separate slot. Do not merge another caller's later
                    # observation into this snapshot if a concurrent read races.
                    if (not isinstance(geometry,dict)
                            or geometry.get('observation_id')!=observation.observation_id
                            or geometry.get('calibration_id')!=observation.calibration_id):
                        raise HarnessError('geometry_context_observation_changed')
                    result['geometry']=pack_geometry(geometry)
                    readiness=status.get('observation_readiness') or {}
                    details=readiness.get('details') or {}
                    if details.get('observation_id')==observation.observation_id:
                        from .conditions import compact_observation_condition
                        result['condition']=compact_observation_condition(readiness)
                return result
            observation=self.call('observe')
            gripper_feedback=None
            if isinstance(observation,dict):
                if observation.get('measured_gripper') is not None:
                    from .gripper_contact import measurement_summary
                    from .contracts import MeasuredGripperFeedback
                    measured=MeasuredGripperFeedback.model_validate(observation['measured_gripper'])
                    gripper_feedback=measurement_summary(measured,
                        min((f['received_ns'] for f in observation.get('frames',())),
                            default=observation['received_ns']))
                observation={k:v for k,v in observation.items()
                    if k not in ('local_collision_evidence','local_collision_evidence_ref',
                                 'measured_gripper','measured_gripper_ref')}
            result=dict(observation=observation,status=self.call('status'))
            if gripper_feedback is not None:result['gripper_feedback']=gripper_feedback
            return result
        if name!='execute_skill' or 'name' not in input_dict or set(input_dict)-{'name','parameters','resume_request_id','verification_request_id'}:
            raise HarnessError('tool_not_available')
        if self.pending:raise HarnessError('unresolved_submission_requires_query')
        # Preparation, revalidation and motion share the original task deadline;
        # a legacy per-skill 30s cap must not terminate a progressing long path.
        deferred=getattr(self,'deferred_grasp',None)
        if deferred:
            if ((deferred['ready'] or deferred.get('relocalized'))
                    and input_dict==deferred.get('arguments',{'name':'approach_grasp'})):
                # Let this cell return. The host will dispatch the exact frozen
                # program in a fresh namespace and bind its own owner receipts.
                return self._needs_attention('approach_grasp','localized_grasp_program_waiting_for_host')
            # A new strategy may run normally. It supersedes any waiting prefix
            # before even a possibly-lost submission is made.
            self.deferred_grasp=None
        maximum=300000
        if getattr(self,'task_deadline',None) is None:self.task_deadline=time.monotonic()+300.
        deadline=self.task_deadline
        # Candidate retries share this read-only deadline, including owner
        # rejections before admission. Never restart 15 seconds for each ID.
        observation_deadline=min(deadline,time.monotonic()+OBSERVATION_READINESS_S)
        original_scope=None;attempt=0;candidate_start=0;skill_deadline=None;excluded_candidates=[]
        last_unpublished=None
        def preparation_failed(reason):
            if last_unpublished is None:
                return self._needs_attention(input_dict['name'],reason,input_dict.get('verification_request_id'),
                    resume_request_id=input_dict.get('resume_request_id'))
            self.attention=dict(skill=input_dict['name'],reason=last_unpublished['reason'],
                                request_id=last_unpublished['request_id'],submitted=True,no_motion=True,
                                observation_retry_reason=reason)
            self._progress('needs_attention',**self.attention)
            store=getattr(self,'evidence_store',None)
            if store is not None:store.event('task_needs_attention',**self.attention)
            return dict(last_unpublished,observation_retry_reason=reason,continuation_submitted=False)
        while attempt<3:
            remaining=deadline-time.monotonic()
            if remaining<=0:return preparation_failed('task_budget_exhausted_before_submission')
            wait_remaining=observation_deadline-time.monotonic()
            if wait_remaining<=0:
                return preparation_failed('observation_readiness_timeout:readiness_budget_exhausted')
            bound = (alignment is not None and input_dict['name']=='align'
                and not input_dict.get('resume_request_id') and not input_dict.get('verification_request_id')
                and time.monotonic() < alignment[1])
            try:
                observation,status=self._await_observation(timeout_s=wait_remaining,expected_scope=original_scope,
                    skill=input_dict['name'],verification=bool(input_dict.get('verification_request_id')),
                    **({'reobserved_alignment':alignment[0]} if bound else {}),
                    **({'viewpoint_handoff':True} if (input_dict.get('resume_request_id')
                       and input_dict['name']=='approach_grasp') or (input_dict['name']=='align'
                       and not input_dict.get('resume_request_id') and not input_dict.get('verification_request_id')
                       and {'move_tcp','reobserve'}.issubset(self.task.allowed_skills)) else {}))
            except ObservationWaitEnded as exc:
                return preparation_failed(str(exc))
            alignment = None
            scope=(self.instance,self.session,status['task_hash'],status['epoch'])
            if original_scope is not None and scope!=original_scope:
                raise HarnessError('replanning_authority_changed')
            original_scope=scope
            remaining_ms=int((deadline-time.monotonic())*1000)-1000
            if remaining_ms<=0:return preparation_failed('task_budget_exhausted_before_submission')
            budget=maximum if status.get('mode')=='hardware' and self.task.goal=='placed' else 10000
            if skill_deadline is None:
                skill_deadline=min(deadline,time.monotonic()+budget/1000)
                deadline=skill_deadline
                observation_deadline=min(observation_deadline,deadline)
            remaining_ms=min(remaining_ms,int((skill_deadline-time.monotonic())*1000))
            if remaining_ms<=0:return preparation_failed('skill_budget_exhausted_before_submission')
            intent=SkillIntent(request_id=uuid.uuid4().hex,session_id=self.session,owner_instance_id=self.instance,
                task_id=self.task.task_id,task_hash=digest(self.task),epoch=status['epoch'],
                observation_id=observation.observation_id,scene_version=observation.scene_version,
                calibration_id=self.task.calibration_id,skill=input_dict['name'],parameters=input_dict.get('parameters',{}),
                resume_request_id=input_dict.get('resume_request_id'),verification_request_id=input_dict.get('verification_request_id'),
                budget_ms=min(budget,remaining_ms))
            intent=intent.model_copy(update={'candidate_start':candidate_start,
                                             'resume_request_id':input_dict.get('resume_request_id'),
                                             'verification_request_id':input_dict.get('verification_request_id'),
                                             'excluded_candidates':tuple(excluded_candidates)})
            self.active_skill=intent.skill
            self._progress('planning',skill=intent.skill,request_id=intent.request_id)
            submitted_at=time.monotonic()
            result=self._submit_and_wait(intent,deadline=deadline)
            self.last_request_chain.append(dict(intent=intent.model_dump(),result=dict(result)))
            if (intent.skill == 'reobserve' and not intent.verification_request_id
                    and result['lifecycle'] == 'completed' and result['outcome'] == 'success'
                    and result.get('no_motion') is True and status.get('alignment_snapshot_id') == observation.observation_id
                    and measured_alignment_pair(observation, self.task)):
                # This reference is consumed by the immediately following tool
                # call. No model-supplied observation can replace the source.
                generation = status['perception_context']['task_generation']
                self._reobserved_alignment = ((observation, intent, generation), time.monotonic()+OBSERVATION_WINDOW_S)
                result = dict(result, alignment_observation=dict(observation_id=observation.observation_id,
                    received_ns=observation.received_ns, hole_id=self.task.hole_id))
            if (result['lifecycle']=='rejected'
                    and (bound and result['reason']=='observation_or_scene_mismatch' or
                         result['reason'] in ('owner_motion_wait','observation_retired_by_motion_wait',
                                            'stale_observation','stale_observation_measurements',
                                            'cartesian_motion_attachment_unknown'))
                    and result['no_motion'] is True):
                # This ID was explicitly never admitted. Preserve the original
                # read-only wait deadline and the two numerical-replan credits.
                if result['reason']=='cartesian_motion_attachment_unknown':
                    self._progress('waiting',skill=intent.skill,reason=result['reason'])
                    time.sleep(min(.05,max(0.,observation_deadline-time.monotonic())))
                continue
            # Only the owner can certify a terminal request had zero publication.
            # Never replay an unknown, cancelled, partially executed or stale-epoch action.
            retry=(result.get('retryable') is True and result.get('no_motion') is True
                   and result['lifecycle']=='completed' and result['outcome']=='failure'
                   and intent.skill in REPLANNABLE_SKILLS)
            if result.get('reason')=='owner_candidate_rejected':
                rejected=result.get('rejected_candidate_variant')
                if type(rejected) is not int or not 0<=rejected<=3:retry=False
                else:
                    excluded_candidates=list(dict.fromkeys(excluded_candidates+[rejected]))
                    retry=retry and len(excluded_candidates)<4
            if not retry or attempt==2:
                if result['outcome']=='success':self.attention=None
                if result['outcome']!='success':
                    self.attention=dict(skill=intent.skill,reason=result['reason'],
                                        submitted=True,no_motion=result.get('no_motion',False))
                    store=getattr(self,'evidence_store',None)
                    if store is not None:store.event('task_needs_attention',**self.attention)
                    result=self._rejected_plan_context(result,intent,deadline=deadline)
                return result
            store=getattr(self,'evidence_store',None)
            if store is not None:
                store.event('unpublished_candidate_retry',request_id=intent.request_id,
                    skill=intent.skill,reason=result.get('reason'),retry_index=attempt+1,
                    same_authority_scope=original_scope)
            # Only accepted, completed, zero-publication planning is excluded
            # from acquisition time. Pre-admission rejection loops still share
            # the original readiness allowance and all work shares deadline.
            observation_deadline=min(deadline,observation_deadline+time.monotonic()-submitted_at)
            last_unpublished=dict(result)
            attempt+=1
        raise HarnessError('unreachable_replanning_state')

    def _rejected_plan_context(self,result,intent,*,deadline):
        """Attach bounded facts without another model-only geometry round.

        Only a known zero-publication plan is eligible. The original owner
        receipt survives a failed optional read; this never retries a motion.
        """
        if (result.get('no_motion') is not True or not result.get('planning_diagnostic')
                or result.get('lifecycle') not in ('completed','rejected')):
            return result
        from .planner_geometry import recovery_geometry
        expected=(self.instance,self.session,intent.task_hash,intent.epoch)
        def checked_status():
            status=self.call('status',timeout_s=min(2.,max(.001,deadline-time.monotonic())))
            context=status.get('perception_context') or {}
            scope=(status.get('owner_instance_id'),context.get('session_id'),
                   status.get('task_hash'),status.get('epoch'))
            if scope!=expected or not status.get('authorized') or status.get('hold_requested'):
                raise HarnessError('recovery_context_authority_changed')
        started=time.monotonic()
        context=dict(state='unavailable',motion_authorized=False)
        try:
            if deadline<=started:raise TimeoutError('recovery_context_budget_exhausted')
            checked_status()
            geometry=self.call('geometry_context',session_id=self.session,
                               timeout_s=min(2.,max(.001,deadline-time.monotonic())))
            if (not isinstance(geometry,dict) or not geometry.get('observation_id')
                    or geometry.get('calibration_id')!=self.task.calibration_id):
                raise HarnessError('recovery_context_identity_invalid')
            checked_status()
            context=dict(state='available',source='owner_geometry_after_zero_publication_rejection',
                geometry=recovery_geometry(geometry),motion_authorized=False)
        except Exception as exc:
            # Do not replace the known action outcome with an ancillary error,
            # nor relabel an IPC/schema/identity failure as ordinary missing pose.
            context.update(error_type=type(exc).__name__,reason=str(exc)[:200],
                           failure_class='context_query_failed')
        store=getattr(self,'evidence_store',None)
        if store is not None:store.diagnostic_event('rejected_plan_context',request_id=intent.request_id,
            task_hash=intent.task_hash,epoch=intent.epoch,context=context,
            elapsed_s=time.monotonic()-started,action_replayed=False)
        return dict(result,recovery_context=context)

    def _submit_and_wait(self,intent,*,deadline):
        deadline=min(deadline,time.monotonic()+intent.budget_ms/1000+1)
        if time.monotonic()>=deadline:raise HarnessError('skill_preparation_budget_exhausted')

        def read(method,**kwargs):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('skill_operation_deadline')
            return self.call(method,timeout_s=min(2.,remaining),**kwargs)

        def checked(payload):
            try:result=SkillResult.model_validate(payload)
            except (ValueError,TypeError):raise HarnessError('skill_result_contract_invalid') from None
            if result.request_id!=intent.request_id:raise HarnessError('skill_result_request_mismatch')
            return result.model_dump()

        self.pending=intent.request_id
        publication_reported=False
        try:
            payload=read('submit_skill',intent=intent.model_dump())
        except Exception:
            # Receipt may be lost after acceptance. Do not retransmit an action.
            try:payload=read('skill_result',session_id=self.session,request_id=self.pending)
            except Exception:raise HarnessError('submission_unknown_stop_and_query') from None
        result=checked(payload)
        while True:
            if (not publication_reported and getattr(self,'_published_request_id',None)==intent.request_id
                    and result['lifecycle'] in ('accepted','running','paused')):
                self._progress('executing',skill=intent.skill,request_id=intent.request_id)
                publication_reported=True
            # A confirmed terminal receipt remains known even if the final read
            # consumed the remaining budget. Do not query it again or replay it.
            if result['lifecycle'] not in ('accepted','running','paused'):
                if result.get('reason') in ('motion_stalled','grasp_not_attached','attachment_lost') and result.get('cancel_stage') != 'measured_stationary':
                    if time.monotonic() >= deadline:
                        break
                    self._progress('waiting', skill=intent.skill, reason=result['reason']+'_waiting_for_measured_hold')
                    read('heartbeat', session_id=self.session)
                    time.sleep(min(.05, max(0., deadline-time.monotonic())))
                    result=checked(read('skill_result',session_id=self.session,request_id=self.pending))
                    continue
                self.pending=None;self.steps+=1
                if intent.skill=='verify_placed' and result['outcome']=='success':
                    self.placement_verified=True
                self._solved=intent.skill==('withdraw_reset' if 'withdraw_reset' in self.task.allowed_skills else 'verify_placed') and result['outcome']=='success' and not result['assisted']
                return result
            if time.monotonic()>=deadline:break
            result=checked(read('skill_result',session_id=self.session,request_id=self.pending))
            if result['lifecycle'] not in ('accepted','running','paused'):continue
            if time.monotonic()>=deadline:break
            read('heartbeat',session_id=self.session)
            time.sleep(min(.05,max(0.,deadline-time.monotonic())))
        # Stopping is bounded separately from the exhausted motion/plan budget;
        # pending remains set until the caller obtains the terminal owner result.
        self.call('cancel_skill',timeout_s=2.,session_id=self.session,request_id=self.pending)
        raise HarnessError('skill_deadline_stop_pending')

    def solved(self):return self._solved
    def env_steps(self):return self.steps
    def artifact(self,name,step=-1):raise HarnessError('image_reference_requires_explicit_fetch')
    def cancel(self):
        if not self._closed:
            try:
                if getattr(self,'defer_session_close',False):
                    # Revocation stays immediate. Only the web supervisor closes
                    # the session, after the owner supplies real hold evidence.
                    if self.pending:
                        try:self.call('cancel_skill',session_id=self.session,request_id=self.pending)
                        except Exception as exc:
                            # Even an unknown request must not prevent task-wide
                            # authority revocation. Never clear it or replay it.
                            store=getattr(self,'evidence_store',None)
                            if store is not None:store.event('remote_cancel_unconfirmed',
                                request_id=self.pending,error=type(exc).__name__,reason=str(exc)[:500])
                    else:
                        finished=getattr(self,'_finished_task_receipt',None)
                        if finished is not None and finished[0]==self.session:
                            return finished[1]
                    return self.call('finish_task',session_id=self.session)
                return self.call('close_session',session_id=self.session)
            finally:self._closed=True
