export const recoveryInstruction='人工控制因方向保护或手柄断线暂停时，松 RB、松开其他按键并让摇杆回中；等输入确认后重新按 RB，再换安全方向移动。暂停期间继续本条录制，START 不负责恢复；自由接入仍不录制。';
export const startInstructions={
 rlt:'START 从 VLA 开始整条；→进入本条的一次局部 RLT。START 人工接管，RB 控臂、RT 控夹爪；人工时松 RB 并等夹爪完成后 START 交回 VLA。正确放置并释放后↑确认局部成功：人工仍保持人工，自主阶段交回 VLA。Y 完成右臂与右夹爪复位、反馈稳定两秒后封存整条。人工暂停时先松 RB 回中，按运行端提示恢复。',
 demonstration:'START 开始一条，直接按 RB 控制右臂；人工阶段 START 不结束，也不恢复暂停。卡住时先松 RB、摇杆回中，等待提示再按 RB，继续同一条。收尾由操作者完成，松 RB、等夹爪操作完成后按十字键↑保存；不要求先回位或等待固定时长。保存完成后整理现场，再 START 开始下一条。',
 play:'START 进入人工接管，直接按 RB 控制右臂，无需选臂。方向保护或手柄断线暂停后，松 RB、摇杆回中，等待提示再按 RB；不要用 START 恢复。自由接入不采集训练图像，↑不分条。',
 policy:'START 开始策略并录制，再按 START 接管，直接 RB 控制右臂；人工阶段 START 不交还，也不恢复暂停。人工暂停后松 RB、摇杆回中，等待提示再按 RB，继续同一条。收尾由操作者完成，松 RB、等夹爪操作完成后按十字键↑保存；不要求先回位或等待固定时长。保存完成再整理现场并 START 下一条。'
};
export const inputWaitLabels={input_wait_reconnect:'链路断开 · 等待重连',input_wait_release_rb:'控制暂停 · 请释放 RB',input_wait_fresh_packet:'等待新鲜输入',input_wait_press_rb:'按 RB 恢复控制',input_wait_feedback:'等待机器人反馈',input_wait_safe_anchor:'当前姿态未通过检查',input_wait_update_sender:'请更新 Windows sender'};
const inputWaitHelp={input_wait_reconnect:'等待 sender 和手柄重新连接；重连后松 RB、松开其他按键并让摇杆回中。',input_wait_release_rb:'松 RB、松开其他按键并让摇杆回中，等待提示后再按 RB。',input_wait_fresh_packet:'保持松 RB、摇杆回中，等待新鲜输入确认。',input_wait_press_rb:'输入已确认。重新按 RB，再换安全方向移动。',input_wait_feedback:'本次 RB 未恢复控制；先松 RB、摇杆回中，等机器人反馈恢复后再按 RB。',input_wait_safe_anchor:'当前实测姿态未通过检查；先松 RB、摇杆回中并排查原因，确认正常后再按 RB。',input_wait_update_sender:'需更新 Windows sender；更新重连后松 RB、摇杆回中，再按 RB。'};
export function recoveryReason(reason){
 const r=String(reason||'');
 if(r.startsWith('manual_candidate_collision'))return '该方向有碰撞风险，候选动作已拦住。';
 if(r.startsWith('manual_candidate_limit'))return '该方向到达限位，候选动作已拦住。';
 if(r==='operator_watchdog'||r==='operator_watchdog_before_publication')return '手柄输入超时，已暂停控制。';
 if(r==='controller_disconnected')return '手柄已断开，已暂停控制。';
 if(r==='operator_disconnected'||r==='operator_session_changed'||r.startsWith('operator_transport_error:'))return '手柄链路中断，已暂停控制。';
 return '';
}
export function inputRecoveryHelp(phase,status={}){
 if(!Object.hasOwn(inputWaitHelp,phase))return null;
 const record=status.recording||{};
 const recording=status.purpose==='teleop_play'?'自由接入不录制。':record.phase==='recording'?'同一条仍在录制；暂停区间会单独标记。':record.phase==='saving'?'本条正在保存。':'';
 return recoveryReason(status.recovery_reason)+inputWaitHelp[phase]+recording+'不要按 START 恢复。';
}
export function hardHoldHelp(status={}){
 const phase=status.recording?.phase;
 if(phase==='recording'&&/^(ik_rejected:|policy_failed:|rtc_)/.test(String(status.hold_reason||'')))return '策略已停止，本条仍在录制。释放控制后按 START 转人工接管，再按 RB 操作；完成后按↑保存。';
 const recording=phase==='saving'?'本条因保护停止正在封存。':phase==='idle'?'当前未录制；保护停止不会续录旧条。':phase==='recording'?'请查看运行终端，确认本条是否已停止；必要时结束运行端。':'';
 return recording+'先松 RB，按原始原因排障；不要反复按 START。确认问题已排除且保存完成后，START 只会开新条。';
}
export const homeInstruction='Y 是可选的快捷回位，与保存本条无关。使用时先手动抬起并移开夹爪，确认回位路径无障碍；松开 RB 后按 Y，右臂直接回积木 demo-V1。Y 不自动避障，左臂与夹爪保持原状态。正在回位时请等待动作结束，再按↑保存，无需额外等待固定时长。';
