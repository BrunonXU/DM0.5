import threading
import time

from f1dagger.rtc_hil_feedback import RTCHILFeedback


class Audio:
    def __init__(self): self.events=[]; self.closed=False
    def notify(self, kind): self.events.append(kind)
    def close(self): self.closed=True


def test_rb_recovery_is_not_a_new_takeover_cue():
    audio=Audio();lines=[]
    f=RTCHILFeedback(audio=audio,report=lines.append,rlt_mode=True)
    try:
        f.notify('input_pause_ended',{'outcome':'rb_rearmed'})
        f.notify('rtc_hil_control_boundary',{'previous_state':'safe_hold','state':'hil_latched'})
        f.jobs.join()
        assert not audio.events
        assert len(lines)==1 and 'RB恢复已受理' in lines[0]
        f.notify('rtc_hil_control_boundary',{'previous_state':'policy','state':'hil_latched'})
        f.jobs.join()
        assert audio.events==['takeover']
    finally:f.close()


def test_candidate_boundary_speaks_once_without_claiming_takeover():
    audio=Audio();lines=[]
    f=RTCHILFeedback(audio=audio,report=lines.append,episode_mode=True,rlt_mode=True)
    try:
        f.notify('input_pause_started',{'reason':'policy_candidate_boundary','requires_start':True})
        f.notify('policy_candidate_paused',{'requires_start':True})
        f.notify('safe_hold_entered',{'reason':'input_wait_start_takeover'})
        f.jobs.join()
        assert audio.events==['motion_limited'] and len(lines)==1
        assert '尚未进入人工控制' in lines[0] and '按START接管' in lines[0]
        f.notify('policy_boundary_takeover_accepted')
        assert audio.events==['motion_limited','takeover']
        f.notify('input_pause_ended',{'outcome':'rb_rearmed'})
        f.notify('rtc_hil_control_boundary',{'previous_state':'safe_hold','state':'hil_latched'})
        f.jobs.join()
        assert audio.events==['motion_limited','takeover']
        assert any('START接管已受理' in line for line in lines)
    finally:f.close()


def test_stationary_policy_timing_pause_is_not_an_accepted_takeover():
    audio=Audio();lines=[]
    f=RTCHILFeedback(audio=audio,report=lines.append,episode_mode=True,rlt_mode=True)
    try:
        f.notify('input_pause_started',{'reason':'stationary_output_timing','requires_start':True})
        f.notify('output_timing_pause_restarted',{'requires_start':True})
        f.jobs.join()
        assert len(lines)==2 and audio.events==[]
        assert all('尚未进入人工控制' in line for line in lines)
        f.notify('policy_boundary_takeover_accepted')
        assert audio.events==['takeover']
        f.notify('output_timing_pause_restarted',{'requires_start':False})
        f.jobs.join()
        assert audio.events==['takeover'] and 'START不恢复' in lines[-1]
    finally:f.close()


def test_rtc_replan_reports_actual_phase_and_cause_without_takeover_cue():
    audio=Audio();lines=[]
    f=RTCHILFeedback(audio=audio,report=lines.append,episode_mode=True,rlt_mode=True)
    try:
        for fields, expected in [
            ({'rlt_phase':'base','reason':'inference_deadline'},'VLA响应迟到'),
            ({'rlt_phase':'actor','reason':'inference_deadline'},'RLT响应迟到'),
            ({'rlt_phase':'actor','reason':'plan_exhausted'},'RLT当前计划已执行完'),
            ({'rlt_phase':'actor','reason':'horizon_coverage'},'RLT计划衔接窗口不足'),
            ({'reason':'inference_deadline'},'策略响应迟到'),
        ]:
            f.notify('rtc_timing_replan',fields);f.jobs.join()
            assert expected in lines[-1] and '同条录制继续' in lines[-1]
        assert not audio.events
    finally:f.close()


def test_manual_reference_timing_pause_does_not_claim_input_disconnected():
    audio=Audio();lines=[]
    f=RTCHILFeedback(audio=audio,report=lines.append,episode_mode=True,rlt_mode=True)
    try:
        f.notify('input_pause_started',{'reason':'runtime_wait:manual_reference',
            'requires_start':False,'recording_continues':True})
        f.jobs.join()
        assert len(lines)==1 and '人工参考／控制节奏变化' in lines[0]
        assert '手柄输入中断' not in lines[0] and '手柄已断开' not in lines[0]
        assert '重新按 RB' in lines[0] and audio.events==[]
    finally:f.close()


def test_rlt_cues_mark_phase_entry_and_fresh_vla_not_each_actor_chunk():
    audio=Audio(); lines=[]
    f=RTCHILFeedback(audio=audio, report=lines.append, episode_mode=True, rlt_mode=True)
    try:
        f.notify('operator_start_authorized')  # Recorder owns the opening cue.
        f.notify('fresh_policy_to_policy')
        f.notify('rlt_switch_rejected')
        f.notify('rlt_switch_accepted')
        for _ in range(3):f.notify('fresh_policy_to_policy')
        f.jobs.join()
        assert audio.events == ['rlt_started']
        f.notify('rtc_hil_control_boundary', {'state':'hil_latched'})
        f.notify('rlt_local_closed', {'status':'success', 'assisted':True})
        f.jobs.join()
        assert audio.events == ['rlt_started','takeover','local_success']
        f.notify('rlt_return_to_vla');f.jobs.join()
        assert audio.events == ['rlt_started','takeover','local_success','resumed']
        assert '准备新观测与策略' in lines[-1]
        f.notify('fresh_policy_to_policy');f.notify('fresh_policy_to_policy');f.jobs.join()
        assert audio.events == ['rlt_started','takeover','local_success','resumed']
        assert any('已进入局部 RLT' in x for x in lines)
        assert any('奖励1已记录' in x and '仍由你控制' in x for x in lines)
        f.notify('rlt_success_rejected');f.jobs.join()
        assert audio.events == ['rlt_started','takeover','local_success','resumed']
        assert any('↑ 未受理' in x for x in lines)
    finally:f.close()


def test_cues_follow_accepted_transitions_and_committed_save():
    audio=Audio(); lines=[]
    f=RTCHILFeedback(audio=audio, report=lines.append)
    f.notify('operator_packet', {'hil_button_pressed':True})
    f.notify('operator_start_authorized')
    f.notify('rtc_hil_control_boundary', {'state':'hil_latched'})
    f.notify('hil_exit_rejected', {'reason':'wait_for_human_hand_motion'})
    f.notify('rtc_hil_control_boundary', {'state':'handoff_hold'})
    f.notify('fresh_policy_to_policy')
    f.notify('session_termination', {'status':'failed'})
    f.jobs.join()
    assert audio.events==['started','takeover','resumed']
    assert any('无需再按START' in x for x in lines)
    f.notify('session_saved'); f.close()
    assert audio.events==['started','takeover','resumed','saved'] and audio.closed


def test_blocked_feedback_cannot_block_control_notifications():
    audio=Audio(); entered=threading.Event(); release=threading.Event()
    def report(_): entered.set(); release.wait(5)
    f=RTCHILFeedback(audio=audio,report=report)
    try:
        f.notify('operator_start_authorized'); assert entered.wait(1)
        begin=time.monotonic()
        for _ in range(100): f.notify('operator_start_authorized')
        assert time.monotonic()-begin<.1 and f.jobs.qsize()<=8
    finally:
        release.set(); f.close()


def test_distinct_cues_use_existing_volume_settings_and_real_wavs(tmp_path):
    import array
    import wave
    from f1dagger.recording_audio import RecordingAudio, prepare_wave, save_levels
    config=tmp_path/'levels.json'
    save_levels(config, {'started':53, 'saved':50})
    calls=[]
    audio=RecordingAudio(config, cue_levels={
        'started':'started','takeover':'started','resumed':'started','saved':'saved',
    }, player=lambda kind,volume:calls.append((kind,volume)))
    feedback=RTCHILFeedback(audio=audio, report=lambda _:None)
    feedback.notify('operator_start_authorized')
    feedback.notify('rtc_hil_control_boundary', {'state':'hil_latched'})
    feedback.notify('rtc_hil_control_boundary', {'state':'handoff_hold'})
    feedback.notify('fresh_policy_to_policy')
    feedback.notify('session_saved')
    feedback.close()
    assert calls==[('started',53),('takeover',53),('resumed',53),('saved',50)]
    for kind,seconds in [('takeover',.92),('resumed',.62),('rlt_started',.525),('local_success',.41)]:
        dest=tmp_path/(kind+'.wav');prepare_wave(kind,53,dest)
        with wave.open(str(dest)) as w:
            assert w.getnchannels()==1 and w.getsampwidth()==2 and w.getframerate()==16000
            assert w.getnframes()==round((seconds+.55)*16000)
            samples=array.array('h',w.readframes(w.getnframes()))
            assert 0<max(map(abs,samples))<32767
            assert not any(samples[:6400]) and not any(samples[-2400:])


def test_pause_recovery_and_hard_stop_have_distinct_operator_instructions():
    audio=Audio(); lines=[]
    feedback=RTCHILFeedback(audio=audio,report=lines.append,episode_mode=True)
    feedback.notify('input_pause_started',dict(reason='manual_candidate_collision',recording_continues=True))
    feedback.notify('safe_hold_entered',dict(reason='input_wait_release_rb'))
    feedback.notify('input_transport_interrupted')
    feedback.notify('input_recovery_ready')
    feedback.notify('input_pause_ended',dict(outcome='rb_rearmed'))
    feedback.notify('episode_start_ignored')
    feedback.notify('safe_hold_entered',dict(reason='feedback_stale'))
    feedback.close()
    assert len(lines)==6
    assert '碰撞风险' in lines[0] and '同一条继续录制' in lines[0]
    assert '等待 sender 和手柄重新连接' in lines[1]
    assert '重新按 RB' in lines[2] and '无需START' in lines[2]
    assert '仍为同一条录制' in lines[3]
    assert 'START未改变当前条' in lines[4]
    assert '保护中断' in lines[5] and 'START开新条' in lines[5]
    assert audio.events==['motion_limited','communication_wait','system_fault']


def test_rtc_policy_failure_preserves_start_to_manual_takeover_instruction():
    audio=Audio(); lines=[]
    feedback=RTCHILFeedback(audio=audio,report=lines.append,episode_mode=True)
    feedback.notify('safe_hold_entered',dict(reason='policy_failed:timeout'))
    feedback.close()
    assert '本条仍在录制' in lines[0] and 'START转人工接管' in lines[0]
    assert 'START开新条' not in lines[0] and audio.events==['system_fault']


def test_rlt_y_rejection_reports_cause_without_success_sound_or_home_promise():
    audio=Audio(); lines=[]
    feedback=RTCHILFeedback(audio=audio,report=lines.append,episode_mode=True,rlt_mode=True)
    try:
        for reason,phase,expected in [
            ('protection_active','idle','不能重新打开记录或绕过保护复位'),
            ('protection_active','recording','当前保护尚未解除'),
            ('policy_recovery_required','recording','按START转人工接管'),
            ('episode_saving','saving','正在封存'),
            ('not_recording','idle','没有进行中的录制'),
            ('manual_pause','recording','人工控制仍在暂停'),
            ('release_rb','recording','先松开RB'),
            ('conflicting_controls','recording','同时改变了START或→'),
        ]:
            feedback.notify('rlt_episode_finish_rejected',dict(reason=reason,episode_phase=phase))
            feedback.jobs.join()
            assert lines[-1].startswith('F1 HIL Y 未受理：') and expected in lines[-1]
            if reason == 'policy_recovery_required':
                assert '本条仍在录制' in lines[-1] and '确认接管后，松开RB再单独按Y' in lines[-1]
                assert '勿反复START' not in lines[-1] and '保护尚未解除' not in lines[-1]
        assert audio.events == []
        assert not any('Y 已受理' in line or '正在复位' in line for line in lines)
    finally:
        feedback.close()


def test_rlt_collision_stop_and_start_rejection_require_ending_current_runtime():
    audio=Audio(); lines=[]
    feedback=RTCHILFeedback(audio=audio,report=lines.append,episode_mode=True,rlt_mode=True)
    try:
        feedback.notify('safe_hold_entered',dict(reason='target_rejected:F1 collision/CoM rejected target: body'))
        feedback.notify('episode_start_rejected',dict(reason='collision_requires_restart'))
        feedback.jobs.join()
        assert len(lines) == 2 and audio.events==['system_fault']
        assert all('退出推理' in line and '恢复姿态后重新启动' in line for line in lines)
        assert 'START开新条' not in lines[0]
        assert '不会新开B1250录制' in lines[1]
    finally:
        feedback.close()



def test_accepted_cue_admission_precedes_slow_console_and_fresh_policy():
    audio=Audio(); entered=threading.Event(); release=threading.Event(); lines=[]
    def slow_report(line):
        lines.append(line);entered.set();assert release.wait(3)
    f=RTCHILFeedback(audio=audio,report=slow_report,episode_mode=True,rlt_mode=True)
    try:
        f.notify('rlt_switch_rejected')
        assert audio.events==[]
        f.notify('rlt_switch_accepted')
        assert audio.events==['rlt_started'] and entered.wait(1)
        f.notify('rlt_return_to_vla')
        assert audio.events==['rlt_started','resumed'] and len(lines)==1
        f.notify('fresh_policy_to_policy')
        assert audio.events==['rlt_started','resumed']
    finally:release.set();f.close()
    assert any('准备新观测与策略' in line for line in lines)


def test_audio_admission_and_teardown_errors_do_not_escape_feedback():
    class BrokenAudio:
        def notify(self,kind):raise OSError('audio unavailable')
        def close(self):raise OSError('audio teardown unavailable')
    lines=[];f=RTCHILFeedback(audio=BrokenAudio(),report=lines.append,episode_mode=True,rlt_mode=True)
    f.notify('rlt_return_to_vla');f.jobs.join();f.close()
    assert len(lines)==1 and '交回 VLA 已受理' in lines[0]
    assert '音频提示不可用' in lines[0]


def test_startup_wait_does_not_instruct_manual_recovery_or_repeat_cue():
    audio=Audio(); lines=[]
    f=RTCHILFeedback(audio=audio,report=lines.append,episode_mode=True,rlt_mode=True)
    try:
        f.notify('input_pause_started',{'reason':'operator_watchdog','startup_policy_wait':True,'requires_start':False})
        f.notify('startup_input_restored')
        f.jobs.join()
        assert len(lines)==2 and not audio.events
        assert '同会话及时恢复后重新观测继续' in lines[0]
        assert '继续本次START启动' in lines[1]
        assert all('START不恢复' not in x for x in lines)
    finally:f.close()


def test_short_prompts_separate_jitter_connection_boundary_and_fault():
    audio=Audio();lines=[]
    f=RTCHILFeedback(audio=audio,report=lines.append,episode_mode=True,rlt_mode=True)
    try:
        for event,fields in [
            ('input_pause_started',{'reason':'operator_watchdog'}),
            ('manual_reference_paused',{}),
            ('input_recovery_ready',{}),
            ('rtc_timing_replan',{'reason':'plan_exhausted'}),
        ]:
            f.notify(event,fields);f.jobs.join()
        assert not audio.events
        for _ in range(2):f.notify('input_transport_interrupted')
        assert audio.events==['communication_wait']
        f.notify('input_pause_ended',{'outcome':'rb_rearmed'})
        for _ in range(2):f.notify('input_pause_started',{'reason':'manual_candidate_collision'})
        assert audio.events==['communication_wait','motion_limited']
        f.notify('recording_failed');f.notify('runtime_fault_paused',{'reason':'writer failed'})
        assert audio.events==['communication_wait','motion_limited','system_fault']
        f.notify('rlt_rating_required');f.notify('rlt_rating_required')
        assert audio.events[-1]=='confirm_rlt' and audio.events.count('confirm_rlt')==1
    finally:f.close()
