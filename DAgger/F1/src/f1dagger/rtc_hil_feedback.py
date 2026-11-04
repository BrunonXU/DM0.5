"""RTC-HIL cues from accepted transitions, outside the control thread."""
import queue
import threading
import time

from .recording_audio import RecordingAudio, POLICY_CUE_LEVELS, VOICE_FILES


class RTCHILFeedback:
    def __init__(self, *, audio=None, report=print, episode_mode=False, rlt_mode=False):
        # Reuse the operator's existing, live-adjustable demo sound levels.
        self.audio = RecordingAudio(cue_levels=POLICY_CUE_LEVELS) if audio is None else audio
        self.exception_key = None
        self.episode_mode = episode_mode
        self.rlt_mode = rlt_mode
        self.awaiting_vla_resume = False
        self.report = report
        self.jobs = queue.Queue(maxsize=8)
        self.closed = False
        self.worker = threading.Thread(target=self._work, name='f1-rtc-hil-feedback', daemon=True)
        self.worker.start()

    def notify(self, event, fields=None):
        fields = fields or {}
        if event in ('policy_input_restored', 'startup_input_restored', 'input_pause_ended',
                     'policy_boundary_takeover_accepted', 'operator_start_authorized', 'rlt_episode_reset'):
            self.exception_key = None
            try:
                getattr(self.audio, 'cancel_exception', lambda: None)()
            except Exception:
                pass
        if event == 'operator_start_authorized':
            self.awaiting_vla_resume = False
        if self.episode_mode and (event == 'operator_start_authorized'
                or (event == 'rtc_hil_control_boundary' and fields.get('state') == 'handoff_hold')):
            return
        if self.episode_mode and event == 'fresh_policy_to_policy':
            if not (self.rlt_mode and self.awaiting_vla_resume):
                return  # Opening cue belongs to the recorder; actor cycles stay quiet.
            self.awaiting_vla_resume = False
        cue = None
        if event == 'rtc_timing_replan':
            phase = {'base':'VLA', 'actor':'RLT'}.get(fields.get('rlt_phase'),
                                                       '策略' if self.rlt_mode else 'VLA')
            cause = {'inference_deadline':'响应迟到', 'late_adoption':'响应迟到',
                     'plan_exhausted':'当前计划已执行完',
                     'horizon_coverage':'计划衔接窗口不足',
                     'ik_window_coverage':'关节计划衔接窗口不足'}.get(
                         fields.get('reason'), '实时衔接需要重新规划')
            text = phase+cause+'，旧动作已撤销；保持并用新观测重新规划，同条录制继续。START仍可接管。'
        elif event == 'runtime_fault_paused':
            text = ('动作已停止，控制台与原始记录保留：' + str(fields.get('reason'))
                    + ('。等待新鲜反馈，恢复后按接管／回中提示操作。' if fields.get('retryable') else
                       '。当前故障已锁存，禁止START／Y启动动作；排查后退出并重新启动。'))
        elif event == 'runtime_feedback_restored':
            text = '反馈恢复；旧动作已撤销，需重新接管／回中确认，不自动继续。'
        elif event == 'manual_reference_paused':
            text = '人工参考／控制节奏变化，已暂停本步，同条继续；松RB回中，再按RB恢复。'
        elif self.rlt_mode and event == 'rlt_local_paused':
            text = '局部放置暂停，同一局部任务保留：' + str(fields.get('reason'))
        elif self.rlt_mode and event == 'rlt_idle_input_wait':
            text = '待机时手柄输入中断；已保存记录不受影响。恢复连接后，再START开始新条。'
        elif self.rlt_mode and event == 'rlt_switch_accepted':
            self.awaiting_vla_resume = False
            text, cue = '已进入局部 RLT：保持后读取新观测；START可人工接管。', 'rlt_started'
        elif self.rlt_mode and event == 'rlt_local_closed' and fields.get('status') == 'success':
            text, cue = ('↑ 已确认：局部放置成功，奖励1已记录；'
                         + ('人工辅助成功，仍由你控制，再START交VLA。' if fields.get('assisted')
                            else '自主成功，正在交回VLA。')), 'local_success'
        elif self.rlt_mode and event == 'rlt_success_rejected':
            text = '↑ 未受理：当前没有可确认的健康局部任务；请检查局部状态与暂停提示。'
        elif self.rlt_mode and event == 'rlt_failure_rejected':
            text = '↓ 未受理：当前没有待评分的局部任务；已有结果不会被覆盖。'
        elif self.rlt_mode and event == 'rlt_rating_required':
            text, cue = '尚未评分：↑确认成功、↓确认失败；仍保持人工。评分后重新按START交回VLA，或按Y复位。', 'confirm_rlt'
        elif self.rlt_mode and event == 'rlt_local_closed':
            text = '局部放置：' + {'success':'成功（人工辅助）' if fields.get('assisted') else '自主成功', 'failure':'已确认失败', 'pending_review':'未按↑，终态已保存待审核；继续交还或复位，不自动记失败0', 'interrupted':'异常中断，待审核'}.get(fields.get('status'), '结果待审')
            if fields.get('status') == 'pending_review':
                cue = 'confirm_rlt'
            elif fields.get('status') == 'failure':
                text = '↓ 已确认局部失败，奖励0已记录。人工仍保持控制；自主策略已停止。START可接管／交还，或按Y复位；现有保护保持。'
        elif self.rlt_mode and event == 'rlt_return_to_vla':
            self.awaiting_vla_resume = True
            text, cue = '交回 VLA 已受理：准备新观测与策略，继续剩余任务；仍可 START 人工纠正。', 'resumed'
        elif self.rlt_mode and event == 'rlt_episode_finish_requested':
            text = 'Y 已受理：右臂复位后张开右夹爪，反馈到位并稳定保持两秒后保存整条。'
        elif self.rlt_mode and event == 'home_gripper_open_requested':
            text = '右臂已回位：正在张开右夹爪，等待实际反馈到位。'
        elif self.rlt_mode and event == 'rlt_episode_finish_rejected':
            reason = fields.get('reason')
            if reason == 'local_rating_required':
                text, cue = 'Y 未受理：请先↑确认局部成功或↓确认失败，再重新按Y复位。START仍可立即接管。', 'confirm_rlt'
            elif reason == 'protection_active':
                detail = ('本条已停止录制，Y不能重新打开记录或绕过保护复位。'
                          if fields.get('episode_phase') != 'recording' else
                          '当前保护尚未解除，Y不会绕过保护启动复位。')
                text = 'Y 未受理：'+detail+'请先按保护提示排查原因，勿反复START或Y。'
            else:
                text = 'Y 未受理：'+{
                    'policy_recovery_required':('策略因推理或IK故障暂停，本条仍在录制；'
                                                '先松开RB、摇杆回中，再按START转人工接管；'
                                                '确认接管后，松开RB再单独按Y。'),
                    'episode_saving':'正在封存本条，Y不会另行启动复位；等待保存完成。',
                    'episode_starting':'正在创建本条，尚不能复位结束；等待录制就绪后重新按Y。',
                    'not_recording':'当前没有进行中的录制，Y不会单独启动复位。',
                    'already_finishing':'本次复位与封存已经受理，等待完成即可。',
                    'manual_pause':'人工控制仍在暂停；先松RB、摇杆回中，按恢复提示重新使能后再按Y。',
                    'release_rb':'先松开RB，再单独按Y。',
                    'release_start':'先松开START，再单独按Y。',
                    'release_success':'先松开↑，再单独按Y。',
                    'release_switch':'先松开→，再单独按Y。',
                    'wait_for_gripper':'等待夹爪命令完成，再单独按Y。',
                    'conflicting_controls':'本次同时改变了START或→，不执行复位；松开按键后单独按Y。',
                }.get(reason, '当前条件不允许复位，请检查控制与录制状态。')
        elif (event == 'rtc_hil_control_boundary' and fields.get('state') == 'hil_latched'
              and fields.get('previous_state') == 'safe_hold'):
            return  # input_pause_ended already reports RB recovery; not START takeover.
        elif self.rlt_mode and event == 'rtc_hil_control_boundary' and fields.get('state') == 'hil_latched':
            text, cue = '人工接管：RB控臂；局部放好先↑，再START交回VLA；Y复位并结束整条。', 'takeover'
        elif event == 'operator_start_authorized':
            text, cue = 'START已受理：策略开始。再按START接管。', 'started'
        elif event == 'rtc_hil_control_boundary' and fields.get('state') == 'hil_latched':
            text, cue = ('人工接管：默认右臂，RB控臂；收尾由操作者完成，松RB并等夹爪完成后↑保存；无需先回位或等待固定时长。' if self.episode_mode else 'START已受理：人工接管。默认右臂，RB控臂；松RB并等夹爪完成后START交还。'), 'takeover'
        elif event == 'rtc_hil_control_boundary' and fields.get('state') == 'handoff_hold':
            text = 'START已受理：正在交还，等待新观测与策略；无需再按START。'
        elif event == 'fresh_policy_to_policy':
            text = '交还完成：新RTC策略开始。'
            cue = None if self.rlt_mode else 'resumed'
        elif event == 'hil_exit_rejected':
            text = 'START交还未受理：' + str(fields.get('reason'))
        elif event == 'policy_candidate_paused':
            text = ('策略候选将越界，已拦截并暂停；同一条继续录制，尚未进入人工控制。'
                    '松RB、摇杆回中后按START接管，再按提示重新使能RB。')
        elif event == 'policy_boundary_takeover_accepted':
            text, cue = ('START接管已受理：松开按键并回中，等输入已确认后重新按RB；'
                         '所有动作仍受保护。'), 'takeover'
        elif ((event == 'input_pause_started' and fields.get('reason') == 'stationary_output_timing')
              or event == 'output_timing_pause_restarted'):
            if fields.get('requires_start'):
                text = ('策略静止等待时输出节奏超时，已暂停并保留同条录制，尚未进入人工控制。'
                        '松RB、摇杆回中后按START接管；等输出与通信确认后，再按RB恢复人工控制。')
            else:
                text = ('静止保持时输出节奏超时，已暂停并保留同条录制。'
                        '松RB、松开其他按键并让摇杆回中；等输出与通信确认后再按RB，START不恢复。')
        elif event == 'startup_input_restored':
            text = '操作连接已恢复，继续本次START启动：读取新观测后运行VLA，无需重复按键。'
        elif event == 'policy_input_restored':
            text = '同会话输入已恢复：旧动作已撤销，重新观测并继续原策略；本段actor不变。'
        elif event == 'input_pause_started' and fields.get('startup_policy_wait'):
            text = '操作连接短暂延迟，机器人保持；同会话及时恢复后重新观测继续，START可接管。'
        elif event == 'input_pause_started':
            reason = str(fields.get('reason') or '')
            if reason == 'policy_candidate_boundary':
                return
            cause = ('该方向有碰撞风险，候选动作已拦住' if reason == 'manual_candidate_collision' else
                     '人工参考／控制节奏变化，本步已暂停' if reason == 'runtime_wait:manual_reference' else
                     '手柄已断开' if reason == 'controller_disconnected' else '手柄输入中断')
            text = (cause+'：松开控制后START转人工，随后松RB回中、再按RB恢复。'
                    if fields.get('requires_start') else
                    cause+'：松 RB、松开其他按键并让摇杆回中，等输入确认后重新按 RB；START不恢复。')
            if self.episode_mode and fields.get('recording_continues'):
                text += '同一条继续录制，暂停区间单独标记。'
        elif event == 'input_transport_interrupted':
            text = '等待 sender 和手柄重新连接；恢复后松 RB、摇杆回中，等输入确认后重新按 RB；不要反复按START。'
        elif event == 'input_recovery_neutral':
            text = '已检测到回中，正在确认往返通信；保持松开，等“输入已确认”后再按RB。'
        elif event == 'input_recovery_ready':
            text = '输入已确认：重新按 RB 恢复人工控制，再换安全方向移动；无需START。'
        elif event == 'input_pause_ended' and fields.get('outcome') == 'rb_rearmed':
            text = 'RB恢复已受理：人工控制继续。'+('仍为同一条录制。' if self.episode_mode else '')
        elif event == 'safe_hold_entered':
            reason = str(fields.get('reason') or '')
            if reason.startswith('input_wait_'):
                return  # The pause/recovery events explain the same boundary.
            if self.rlt_mode and reason.startswith('target_rejected:F1 collision/CoM rejected target:'):
                text = ('碰撞保护停止：'+reason+'。本条按中断封存，当前会话已锁定；'
                        '先退出推理，按现场安全流程恢复姿态后重新启动。不要反复START或Y恢复。')
            elif self.episode_mode and reason.startswith(('ik_rejected:', 'policy_failed:', 'rtc_')):
                text = '策略停止：'+reason+'。本条仍在录制；释放控制后START转人工接管，完成后↑保存。'
            else:
                text = '保护停止：'+reason+'。先松 RB 并排查原因；不要反复按START。'
                if self.episode_mode:
                    text += '本条按保护中断处理；确认保存完成并排除问题后，START开新条。'
        elif event == 'episode_end_rejected':
            text = '↑未结束：'+{'release_rb':'先松开 RB', 'wait_for_home':'等待当前正在执行的 Y 回位结束',
                'wait_for_gripper':'等待夹爪完成', 'release_start':'先松开 START',
                'not_recording':'当前未在录制'}.get(fields.get('reason'), str(fields.get('reason')))+'；条件就绪后重新按↑。'
        elif event == 'episode_start_ignored':
            text = 'START未改变当前条：人工暂停时松 RB、摇杆回中，等输入确认后重新按 RB；结束本条按↑，保存中请等待。'
        elif self.rlt_mode and event == 'episode_start_rejected' and fields.get('reason') == 'collision_requires_restart':
            text = ('START 未受理：碰撞保护仍锁定。先退出推理，按现场安全流程恢复姿态后重新启动；'
                    '不能在本会话反复START或Y恢复，不会新开B1250录制。')
        elif event == 'recording_failed':
            text, cue = ('录制失败，请停止采集。自动策略和Home已禁用；反馈及保护健康时可START接管、重新RB人工退场，随后Ctrl+C。', 'system_fault')
        elif event == 'recording_failed_operation_rejected':
            text = '本条录制失败：仅允许人工安全退场，不能交回策略或Y复位。退场后Ctrl+C并修复存储。'
        elif event == 'session_saved':
            text, cue = '原始会话已完整保存；训练审核另行进行。', 'saved'
        else:
            return
        if self.rlt_mode:
            text = text.replace('完成后↑保存', '放好后↑标成功，START交回VLA；Y结束整条').replace('结束本条按↑', '结束本条按Y').replace('↑未结束', '↑成功标记未受理')
        if self.closed:
            return
        if cue is None:
            if event == 'policy_candidate_paused':
                cue = 'motion_limited'
            elif event == 'runtime_fault_paused':
                cue = 'system_fault'
            elif event == 'input_pause_started' and not fields.get('startup_policy_wait'):
                reason = str(fields.get('reason') or '')
                if reason == 'manual_candidate_collision':
                    cue = 'motion_limited'
                elif reason == 'controller_disconnected':
                    cue = 'communication_wait'
                # Input jitter, timing waits and RB recovery stay on screen.
            elif event == 'input_transport_interrupted':
                cue = 'communication_wait'
            elif event == 'safe_hold_entered':
                cue = 'system_fault'
        if cue in VOICE_FILES:
            key = (cue, fields.get('session_id'), fields.get('segment_id'), fields.get('policy_epoch'))
            if key == self.exception_key:
                cue = None
            else:
                self.exception_key = key
        # Audio admission is nonblocking and precedes the console queue. A
        # slow terminal must not postpone an already accepted phase cue.
        if cue:
            try:
                self.audio.notify(cue)
            except Exception:
                text += ' 音频提示不可用，请以控制台状态为准。'
        try:
            self.jobs.put_nowait((text, time.monotonic()))
        except queue.Full:
            pass

    def _work(self):
        while True:
            item = self.jobs.get()
            try:
                if item is None:
                    return
                text, created = item
                if time.monotonic()-created <= 2:
                    self.report('F1 HIL '+text)
            except Exception:
                pass  # A failed console/audio path never changes control.
            finally:
                self.jobs.task_done()

    def close(self):
        self.closed = True
        try:
            self.jobs.put(None, timeout=1)
        except queue.Full:
            self._close_audio()
            return
        self.worker.join(2)
        self._close_audio()

    def _close_audio(self):
        try:
            self.audio.close()
        except Exception:
            pass  # Audio teardown cannot change the control/save outcome.
