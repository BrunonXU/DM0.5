"""Single-flight H50 prefetch at 30 Hz, patterned after G2 powered-async.

No boundary blend, smoothing, velocity extrapolation, or forced inter-chunk
stop. Only unexpired model points are executed. Exhaustion/fault stops via the
shared runtime cleanup. ROS and command publication stay on the main thread.
"""
import hashlib,json,math,time,uuid
from .execution import prepare_prefix,IKPrefixRejected
from .observation import CameraObservationError
from f1_replay.runtime import ReplayError

MODE='async_native_h50_v1'
PERIOD_NS=round(1e9/30)


def activate_with_ik_retry(attempt, retry_limit, can_request, on_retry):
    """Retry only checked IK rejections; attempt must capture a NEW observation.

    Never accept partial commands or reinterpret validation/transport failures.
    The caller is between prefixes with zero base command and last arm target.
    """
    if type(retry_limit) is not int or not 0 <= retry_limit <= 3:
        raise ReplayError('IK重采次数必须为0..3整数')
    for index in range(retry_limit + 1):
        try:
            return attempt()
        except IKPrefixRejected as exc:
            if index == retry_limit or not can_request():
                raise
            on_retry(index + 1, exc)


def activate_with_ik_pause(attempt,retry_limit,can_request,on_retry,on_pause):
    """Each retry burst is bounded. Only an explicit operator resume starts another."""
    while True:
        try:
            return activate_with_ik_retry(attempt,retry_limit,can_request,on_retry)
        except IKPrefixRejected as exc:
            if not can_request():raise
            on_pause(exc)  # Must keep polling and may raise on cancellation/fault.


def wait_ik_resume(backend,tick,log,result,*,powered,wait_for_enter):
    """Pause at current feedback without restarting the run, recorder or home sequence."""
    tick()
    if powered:
        zero=backend.stop_base();hold=backend.hold_arms()
        if zero is None or hold is None:raise ReplayError('IK暂停未成功发布底盘零速/当前手臂保持')
        log('ik_pause_hold',{'base':zero,'arms':hold})
    result['ik_pauses']+=1
    log('ik_paused',{'pause':result['ik_pauses'],'requires_operator_resume':True,'automatic_home':False})
    print('IK连续失败，已暂停在当前位置。按 Enter 重新取图继续；Ctrl-C 结束。无需自动回home。',flush=True)
    last_zero=time.monotonic()
    def poll():
        nonlocal last_zero
        tick()
        if powered and time.monotonic()-last_zero>=.1:
            if backend.stop_base() is None:raise ReplayError('IK暂停底盘零速发布失败')
            last_zero=time.monotonic()
        return False
    wait_for_enter(poll)
    tick()
    log('ik_resumed',{'pause':result['ik_pauses'],'policy':'capture_new_observation_and_predict'})

def fresh_start(observation_ns,activation_ns,min_fresh_steps):
    if activation_ns<observation_ns:raise ReplayError('连续模式观测时钟在未来')
    index=math.ceil((activation_ns-observation_ns)*30/1e9)
    if 50-index<min_fresh_steps:raise ReplayError(f'连续响应已过时：跳过{index}点后不足{min_fresh_steps}个新鲜点')
    return index


class Timeline:
    """A single active queue; no implicit repetition when its model points end."""
    def __init__(self):
        self.frames=[];self.cursor=0;self.playback_id=0;self.next_ns=0
        self.request_id=None;self.start_index=0
    @property
    def remaining(self):return len(self.frames)-self.cursor
    def activate(self,prepared,request_id,start_index,now_ns,reset_clock=False):
        if not self.frames or reset_clock:self.next_ns=now_ns
        self.frames=prepared.bundle.frames;self.cursor=0;self.playback_id+=1
        self.request_id=request_id;self.start_index=start_index
    def due(self,now_ns,max_lateness_ns):
        if now_ns<self.next_ns:return None
        if not self.remaining:raise ReplayError('连续动作队列耗尽，下一段尚未接上；已停止，不延长最后一个速度命令')
        if now_ns-self.next_ns>max_lateness_ns:raise ReplayError('连续控制调度超时；不追赶补发')
        frame=self.frames[self.cursor];index=self.start_index+self.cursor
        self.cursor+=1
        # Never burst multiple model points to catch up a late tick.
        self.next_ns=max(self.next_ns+PERIOD_NS,now_ns+PERIOD_NS)
        return frame,index


def run_loop(doc,profile,backend,policy,pool,output,log,tick,publish,result,*,powered,chunks):
    from .runtime import observe,wait_camera_observation,wait_request
    cfg=doc['client'];timeline=Timeline();pending=None
    synchronous=cfg['inter_chunk_policy'] in ('sync_raw_h50_v1','sync_raw_h20_v1')
    decoded_steps=cfg['execute_steps']
    binding=result.pop('_native_binding')
    capability=result.pop('_native_capability')
    if decoded_steps==50 and capability!=MODE:raise ReplayError('服务端不支持连续H50动作')
    result.update(inter_chunk_policy=cfg['inter_chunk_policy'],
                  chunk_count_semantics=f'complete_{decoded_steps}_point_prefixes_of50' if synchronous else 'activated_prefixes; async switch may truncate previous horizon',
                  model_horizon=50, executed_prefix_steps=decoded_steps,
                  native_switches=0,native_points_published=0,forced_inter_chunk_stops=0,
                  full_horizons_executed=0,completed_prefixes=0,stop_confirmation_waits=0)
    result['ik_retries']=0
    retry_limit=cfg.get('ik_replan_retry_limit',0)
    if type(retry_limit) is not int or not 0<=retry_limit<=3 or (retry_limit and not synchronous):
        raise ReplayError('IK重采仅支持同步raw20/raw50，次数0..3')
    from .execution import PREFIX_POLICY
    variable=profile.get('inference_prefix_policy')==PREFIX_POLICY
    pause=cfg.get('ik_pause_on_exhausted',False)
    if type(pause) is not bool or (pause and (not variable or not synchronous or decoded_steps!=20)):
        raise ReplayError('IK暂停恢复需要可变前缀同步H20')
    result.update(ik_pauses=0,partial_prefixes=0)
    if variable:
        result['chunk_count_semantics']='requested_predictions; each accepted prefix has 1..20 original points'
    def budget():return chunks==0 or result['chunks_requested']<chunks
    def submit(observation,source_playback):
        request_id=uuid.uuid4().hex;directory=output/request_id
        images={k:{'bytes':len(v),'sha256':hashlib.sha256(v).hexdigest()} for k,v in observation.images.items()}
        log('observation',{'request_id':request_id,'observation_monotonic_ns':observation.captured_ns,
            'state19':list(observation.state19),'body5':list(observation.body5),
            'cameras':observation.camera_metadata,'images':images,'source_playback_id':source_playback})
        def work():
            directory.mkdir()
            for name,data in observation.images.items():(directory/(name+'.image')).write_bytes(data)
            response=policy.infer(observation,request_id)
            (directory/'response.json').write_text(json.dumps(response,indent=2,allow_nan=False)+'\n')
            return response
        future=pool.submit(work);result['chunks_requested']+=1
        return dict(future=future,observation=observation,request_id=request_id,source=source_playback)
    def activate(request,response,first=False):
        if not first and request['source']!=timeline.playback_id:raise ReplayError('连续响应所属播放段已经失效')
        reference_snapshot=backend.snapshot()
        now=time.monotonic_ns()
        start=0 if first else fresh_start(request['observation'].captured_ns,max(now,timeline.next_ns),cfg['min_fresh_steps'])
        try:
            prepared=prepare_prefix(response,request['observation'],reference_snapshot,profile,
                now_ns=now,request_id=request['request_id'],expected_model_binding=binding,
                max_observation_age_s=cfg['max_observation_age_s'],execute_steps=decoded_steps,start_index=start)
        except (ReplayError,ValueError) as exc:
            result['chunks_rejected']+=1;result['rejection_reasons'][str(exc)]=result['rejection_reasons'].get(str(exc),0)+1
            log('chunk_rejected',{'request_id':request['request_id'],'reason':str(exc),'start_index':start})
            raise
        checked=time.monotonic_ns()
        if not first and fresh_start(request['observation'].captured_ns,max(checked,timeline.next_ns),cfg['min_fresh_steps'])!=start:
            # Revalidate selected boundary if parsing crossed a model timestamp.
            start=fresh_start(request['observation'].captured_ns,max(checked,timeline.next_ns),cfg['min_fresh_steps'])
            reference_snapshot=backend.snapshot()
            prepared=prepare_prefix(response,request['observation'],reference_snapshot,profile,
                now_ns=time.monotonic_ns(),request_id=request['request_id'],expected_model_binding=binding,
                max_observation_age_s=cfg['max_observation_age_s'],execute_steps=decoded_steps,start_index=start)
        previous_remaining=timeline.remaining
        timeline.activate(prepared,request['request_id'],start,time.monotonic_ns(),reset_clock=first)
        result['chunks_accepted']+=1;result['native_switches']+=int(not first)
        result['partial_prefixes']+=int(len(prepared.bundle.frames)<decoded_steps and synchronous)
        log('chunk_accepted',{'request_id':request['request_id'],'playback_id':timeline.playback_id,
            'start_index':start,'skipped_stale_steps':start,'fresh_steps':timeline.remaining,
            'previous_unexecuted_steps':previous_remaining,'observation_age_s':(now-request['observation'].captured_ns)/1e9,
            'first_arm_delta_deg':list(prepared.first_arm_delta_deg),'gripper_mapping':list(prepared.gripper_mapping),
            'first_arm_policy':profile.get('inference_first_arm_policy','reject'),
            'valid_steps':len(prepared.bundle.frames),'truncation':response.get('truncation'),
            'commands19':[list(f.action) for f in prepared.bundle.frames],
            'native_adapter_timing_s':response.get('native_adapter_timing_s')})
    def request_sync(source):
        def attempt():
            observation=wait_camera_observation(backend,cfg,profile,tick,log)
            request=submit(observation,source)
            response=wait_request(request['future'],tick,cfg['request_timeout_s'])
            activate(request,response,True)
            return request
        def on_retry(index,exc):
            result['ik_retries']+=1
            log('ik_replan_retry',{'attempt':index,'limit':retry_limit,'ik':exc.ik,
                'policy':'discard_entire_rejected_prefix_and_capture_new_observation',
                'base_policy':'zero_command','arm_policy':'retain_last_target'})
            if powered:
                sent=backend.stop_base()
                if sent is None:raise ReplayError('IK重采前底盘零速未发布')
                log('ik_retry_base_zero',sent)
        if pause:
            from .runtime import wait_for_enter
            def on_pause(exc):
                log('ik_retry_exhausted',{'ik':exc.ik,'retry_limit':retry_limit})
                wait_ik_resume(backend,tick,log,result,powered=powered,wait_for_enter=wait_for_enter)
            return activate_with_ik_pause(attempt,retry_limit,budget,on_retry,on_pause)
        return activate_with_ik_retry(attempt,retry_limit,budget,on_retry)
    request_sync(0)
    last_camera_log=0
    while True:
        tick();now=time.monotonic_ns()
        # At horizon end stop; a response that missed the deadline cannot restart
        # the chassis after its queue has already expired.
        if not timeline.remaining and now>=timeline.next_ns:
            result['completed_prefixes']+=1
            if decoded_steps==50:result['full_horizons_executed']+=1
            if pending is None and not budget():break
            if synchronous:
                # The model horizon is exhausted. Never extrapolate its final
                # velocity through the HTTP wait; request immediately, without
                # waiting for measured standstill or changing arm targets.
                if powered:
                    sent=backend.stop_base()
                    if sent is None:raise ReplayError(f'模型{decoded_steps}点执行结束后的底盘零速未发布')
                    log(f'raw{decoded_steps}_inference_gap_zero',sent)
                    result['forced_inter_chunk_stops']+=1
                gap_started=time.monotonic_ns()
                following=request_sync(timeline.playback_id)
                log(f'raw{decoded_steps}_inference_gap',{'elapsed_s':(time.monotonic_ns()-gap_started)/1e9,
                    'arm_policy':'retain_last_model_position_target','base_policy':'zero_once_no_standstill_gate',
                    'request_id':following['request_id']})
                continue
            raise ReplayError('连续动作队列耗尽，下一段未及时接上；不延长末帧或自动重启')
        if timeline.remaining:
            item=timeline.due(now,round(profile['max_lateness_s']*1e9))
            if item:
                frame,index=item
                if powered:
                    sent=publish(frame.action)
                    result['native_points_published']+=1
                    if timeline.cursor==1:result['chunks_executed']+=1
                    log('command',{'request_id':timeline.request_id,'playback_id':timeline.playback_id,
                        'model_index':index,'action':list(frame.action),'publication':sent})
                else:
                    log('shadow_command',{'request_id':timeline.request_id,'model_index':index,'action':list(frame.action)})
        if pending is not None and pending['future'].done():
            response=pending['future'].result()
            activate(pending,response);pending=None
        if not synchronous and pending is None and budget() and timeline.remaining<=cfg['prefetch_lookahead_steps']:
            try:observation=observe(backend,cfg,profile)
            except CameraObservationError as exc:
                if time.monotonic()-last_camera_log>.2:
                    log('native_camera_wait',{'reason':str(exc),'remaining_points':timeline.remaining});last_camera_log=time.monotonic()
            else:
                pending=submit(observation,timeline.playback_id)
                log('native_prefetch',{'request_id':pending['request_id'],'source_playback_id':timeline.playback_id,
                    'remaining_points':timeline.remaining})
        if pending is not None and (time.monotonic_ns()-pending['observation'].captured_ns)>cfg['request_timeout_s']*1e9:
            raise ReplayError('连续推理HTTP超时')
        time.sleep(min(.002,max(0.,(timeline.next_ns-time.monotonic_ns())/1e9)))
