"""One explicit pose, all-part preflight, common timebase and bounded execution.
Continuous trajectories use internal feedback checkpoints, without user stages.
Optional head boundary return is separately confirmed before normal planning.
No automatic obstacle routing. Imports never connect to ROS.
"""
import math
from copy import deepcopy
from .motion_plan import SmoothBodyPlan,BodyCadence
from .waist_kinematics import CartesianBodyPlan
from . import head_boundary_recovery

PARTS={'left_arm':tuple(f'arm_l_j{i}' for i in range(1,8)), 'right_arm':tuple(f'arm_r_j{i}' for i in range(1,8)),
       'body':tuple(f'body_j{i}' for i in range(1,6)), 'head':tuple(f'head_j{i}' for i in range(1,4))}
LABELS={'left_arm':'左臂','right_arm':'右臂','body':'身体','head':'头部','left_gripper':'左夹爪','right_gripper':'右夹爪'}

def task_limits(a):
    """Independent total-task ceiling; clients cannot set speed or duration.

    Six seconds bounds internal monitoring windows, not the user's whole task.
    Sixty seconds covers configured full joint travel without unbounded execution.
    Legacy joints/lift/base calls continue to use their original six-second budget.
    """
    configured=a.profile['max_action_s']
    if isinstance(configured,bool) or not isinstance(configured,(int,float)) or not math.isfinite(configured) or configured<=.3:
        raise ValueError('姿态检查周期配置无效')
    checkpoint=min(6.,float(configured))
    maximum=a.profile.get('max_pose_task_s',60.)
    if isinstance(maximum,bool) or not isinstance(maximum,(int,float)) or not math.isfinite(maximum) or not checkpoint<=maximum<=60.:
        raise ValueError('整套姿态任务时限配置无效')
    return checkpoint,float(maximum)


def source_keys(public):
    """Guard every selected topic, even when a selected part is already there."""
    return (['joint_command'] if public['targets'] else []) + [side+'_gripper_command' for side in public['grippers']]


def execution_deadline(a,public,task_start):
    """Budget the latest complete plan, bounded by the original task ceiling."""
    now=a.clock();maximum=task_limits(a)[1]
    deadline=min(task_start+maximum,now+public['max_action_s'])
    required=public['motion_s']+.15+public.get('head_boundary_recovery',{}).get('timeout_s',0.)
    if now+required>deadline:
        raise ValueError('准备后剩余任务时间不足以完成整套姿态，本次尚未发送动作')
    return deadline


def build(a,spec,snap):
    checkpoint,maximum=task_limits(a)
    motion_budget=maximum-.3
    targets=spec.get('targets',{});grips=spec.get('grippers',{})
    if not isinstance(targets,dict) or not isinstance(grips,dict) or not (targets or grips or 'waist_delta_m' in spec):raise ValueError('请先编辑姿态或载入预设')
    if any(s not in ('left','right') for s in grips):raise ValueError('未知夹爪')
    targets=dict(targets);waist=None
    recovery=head_boundary_recovery.inspect(a.profile,targets,snap,a._fresh)
    recovery_budget=recovery['timeout_s'] if recovery else 0.
    motion_budget-=recovery_budget
    exceptional={recovery['joint']:recovery['origin']} if recovery else None
    if 'waist_delta_m' in spec:
        if any(n in targets for n in ('body_j3','body_j4','body_j5')):raise ValueError('升降与身体角度不能同时作为目标，请选择一种编辑方式')
        q={n:max(a.profile['joint_limits_deg'][n][0],min(a.profile['joint_limits_deg'][n][1],a._fresh(snap['joints'].get(n),n)['position'])) for n in PARTS['body']}
        # Reuse every original freshness, boundary and model path check.
        _,body,waist=a._joint_target({'targets':q,'waist_delta_m':spec['waist_delta_m']},snap,max_motion_s=motion_budget)
        targets={**body,**targets}
    origin={};joint_plan=None
    if targets:origin,targets,joint_plan=a._joint_target({'targets':targets},snap,coordinated=True,max_motion_s=motion_budget,exceptional_origins=exceptional)
    grip_origin={}
    for side,value in grips.items():
        if not a.profile['gripper_verified']:raise ValueError('夹爪合同未核验')
        if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or not 0<=value<=100:raise ValueError('夹爪目标必须为0–100')
        grip_origin[side]=a._fresh(snap['grippers'].get(side),side+'夹爪')['position']
    parts=[];moving=set()
    for part,names in PARTS.items():
        selected=[n for n in names if n in targets]
        if not selected:continue
        done=all(abs(snap['joints'][n]['position']-targets[n])<=a.profile['joint_tolerance_deg'] for n in selected)
        parts.append({'id':part,'label':LABELS[part],'status':'at_target' if done else 'pending'})
        if not done:moving.add(part)
    for side,value in grips.items():
        part=side+'_gripper';done=abs(value-grip_origin[side])<=a.profile['gripper_tolerance']
        parts.append({'id':part,'label':LABELS[part],'status':'at_target' if done else 'pending'})
        if not done:moving.add(part)
    # Plan a single synchronized fraction for all units; body keeps the existing
    # 200Hz path. Arms and grippers are sent at 30Hz on that same clock.
    all_origin={**origin,**{'gripper_'+s:v for s,v in grip_origin.items()}}
    all_targets={**targets,**{'gripper_'+s:v for s,v in grips.items()}}
    speed={n:25. if n.startswith('gripper_') else a.profile['max_joint_speed_deg_s'] if n.startswith('arm_') else a.profile['body_speed_deg_s'] for n in all_targets}
    accel={n:100. if n.startswith('gripper_') else a.profile['max_joint_acceleration_deg_s2'] if n.startswith('arm_') else a.profile['body_acceleration_deg_s2'] for n in all_targets}
    plan=SmoothBodyPlan(all_origin,all_targets,speed,accel,motion_budget)
    if waist:plan.duration=max(plan.duration,waist.duration);waist.duration=plan.duration
    if waist and a.profile.get('ground_guard'):
        from .geometry import check_ground_path,model
        actual={n:a._fresh(snap['joints'].get(n),n)['position'] for n in model()['joint_map']}
        for i in range(1,21):
            row={n:v for n,v in plan.sample(plan.duration*i/20).items() if not n.startswith('gripper_')}
            row.update({n:v for n,v in waist.sample(plan.duration*i/20).items() if n in ('body_j3','body_j4','body_j5')})
            check_ground_path(actual,row);actual.update(row)
    keys=(['joint_command'] if any(x in moving for x in PARTS) else [])+[s+'_gripper_command' for s in grips if s+'_gripper' in moving]
    if recovery and 'joint_command' not in keys:keys.append('joint_command')
    if recovery and a.profile.get('ground_guard'):
        from .geometry import check_ground_path,model
        actual={n:a._fresh(snap['joints'].get(n),n)['position'] for n in model()['joint_map']}
        check_ground_path(actual,{recovery['joint']:recovery['edge']})
    for key in source_keys({'targets':targets,'grippers':grips}):a.transport.assert_exclusive(key)
    motion_s=plan.duration if moving else 0.
    deadline=min(maximum,max(checkpoint,recovery_budget+motion_s+a.profile['settle_timeout_s']))
    public={'targets':targets,'grippers':grips,'parts':parts,'motion_s':motion_s,
            'estimated_total_s':recovery_budget+motion_s+.15,'max_action_s':deadline,
            'checkpoint_budget_s':checkpoint,'checkpoint_count':max(0,math.ceil(motion_s/(checkpoint-.3))-1) if moving else 0,
            'no_motion':not moving and not recovery,'mode':'boundary_then_synchronized' if recovery else 'synchronized','scope_complete':True}
    if recovery:public['head_boundary_recovery']=recovery

    return public,plan,waist,moving,keys

def feedback(a,snap,public):
    items={};states=[]
    for part in public['parts']:
        key=part['id']
        if key in PARTS:
            names=[n for n in PARTS[key] if n in public['targets']]
            row={n:a._fresh(snap['joints'].get(n),n) for n in names}
            done=all(abs(row[n]['position']-public['targets'][n])<=a.profile['joint_tolerance_deg'] for n in names)
        else:
            side=key.split('_')[0];row={key:a._fresh(snap['grippers'].get(side),LABELS[key])}
            done=abs(row[key]['position']-public['grippers'][side])<=a.profile['gripper_tolerance']
        items.update(row);states.append({'id':key,'label':LABELS[key],'status':'at_target' if done else 'moving'})
    return items,states


def arrival_feedback(a,public,items,now):
    """Copy the exact ROS samples that passed the final arrival check.

    Joint samples are HAL receipts; neither their receipt sequence nor their
    source stamp independently verifies encoder/PDO freshness or calibration.
    Construct this evidence only on success, without another snapshot read.
    """
    def row(target,item,unit,tolerance):
        value={'unit':unit,'target':target,'actual':item['position'],
               'error':item['position']-target,'tolerance':tolerance,
               'age_s':item['age_s'],'receipt_seq':item['receipt_seq']}
        if item.get('source_stamp') is not None:
            value['source_stamp']=deepcopy(item['source_stamp'])
        return value
    return {'scope':'selected_targets_only','basis':'received_ros_feedback',
            'freshness_scope':'gateway_receipt_only','error_convention':'actual_minus_target',
            'observed_monotonic_s':now,'minimum_confirmation_s':.15,
            'profile':{key:a.profile[key] for key in ('id','version') if key in a.profile},
            'joints':{n:row(target,items[n],'deg',a.profile['joint_tolerance_deg']) for n,target in public['targets'].items()},
            'grippers':{side:row(target,items[side+'_gripper'],'0-100',a.profile['gripper_tolerance']) for side,target in public['grippers'].items()}}


def sample(plan,waist,elapsed):
    values=plan.sample(elapsed)
    if waist:values.update({n:v for n,v in waist.sample(elapsed).items() if n in ('body_j3','body_j4','body_j5')})
    return values


def checkpoint_feedback(a,snap,plan,waist,elapsed,parts):
    """Verify actual progress without stopping or resetting trajectory velocity.

    Compare with the planned path at the sample's receipt time, allowing one
    existing feedback-freshness window and existing arrival tolerance. This
    catches stalled fresh feedback; it is not a collision or servo guarantee.
    """
    for part in parts:
        if part in PARTS:
            rows=[(n,a._fresh(snap['joints'].get(n),n),a.profile['joint_tolerance_deg']) for n in PARTS[part] if n in plan.targets]
        else:
            side=part.split('_')[0]
            rows=[('gripper_'+side,a._fresh(snap['grippers'].get(side),LABELS[part]),a.profile['gripper_tolerance'])]
        for name,row,tolerance in rows:
            at=max(0.,elapsed-row['age_s']);lag=a.profile['feedback_max_age_s']
            before=sample(plan,waist,max(0.,at-lag))[name]
            after=sample(plan,waist,min(plan.duration,at+lag))[name]
            if not min(before,after)-tolerance<=row['position']<=max(before,after)+tolerance:
                raise ValueError(f'{LABELS[part]}未跟上预期动作，已停止整套姿态；请检查实测后重新执行')



def observe_no_motion(a,public,cancel,on_progress=None,guard_keys=(),task_deadline=None,after_recovery=False):
    """Confirm an already reached target without creating a command endpoint.

    A single preflight sample is not an arrival confirmation. Observe distinct
    incoming feedback spanning the same 150ms used after commanded motion.
    Drift during observation never silently turns this into a moving task.
    """
    start=a.clock();arrival=None;parts=public['parts']
    deadline=min(public['max_action_s'],a.profile['settle_timeout_s'])
    if task_deadline is not None:deadline=min(deadline,task_deadline-start)
    while not cancel.is_set():
        for key in guard_keys:a.transport.assert_exclusive(key)
        snap=a._common();now=a.clock();elapsed=now-start
        if elapsed>deadline:raise ValueError('尚未收到连续到位反馈，请检查实测后重新执行')
        items,parts=feedback(a,snap,public)
        if any(p['status']!='at_target' for p in parts):
            error=ValueError('回程后实测姿态已改变，后续动作未继续；请重新执行' if after_recovery else '确认期间实测姿态已改变，本次没有发送动作；请重新执行')
            error.parts=parts
            raise error
        receipts={n:(x.get('receipt_seq'),now-x['age_s']) for n,x in items.items()}
        if any(seq is None for seq,_ in receipts.values()):raise ValueError('反馈缺少接收序号，无法确认整套到位')
        if arrival is None:arrival=receipts
        confirmed=all(seq!=arrival[n][0] and at-arrival[n][1]>=.15 for n,(seq,at) in receipts.items())
        if on_progress:on_progress({'elapsed_s':elapsed,'progress':min(1.,elapsed/.15),'parts':parts,'feedback_arrived':confirmed,'estimated_total_s':.15})
        if cancel.is_set():break
        if confirmed:return {'status':'succeeded','no_motion':True,'commands_sent':False,'feedback_verified':True,'parts':parts,'elapsed_s':elapsed,
                             'arrival_feedback':arrival_feedback(a,public,items,now)}
        cancel.wait(1/a.profile['command_hz'])
    return {'status':'cancelled','no_motion':True,'commands_sent':False,'feedback_verified':False,'parts':parts,'elapsed_s':a.clock()-start}


def execute(a,spec,cancel,on_progress=None):
    if not a._action_lock.acquire(blocking=False):raise ValueError('已有动作正在执行')
    a._active_cancel=cancel;prepared=[];sent_keys=set();last_parts=[];cadence=None;held=[]
    recovery_result=None;task_start=a.clock();task_deadline=None
    def acknowledge(public):
        if public.get('head_boundary_recovery') and spec.get('head_boundary_recovery_ack') is not True:
            raise ValueError('头部当前在工作范围外，请先确认向内回程')
    def finish(result):
        if recovery_result is not None:
            result={**result,'head_boundary_recovery':recovery_result,
                    'commands_sent':bool(result.get('commands_sent') or recovery_result['commands_sent']),
                    'elapsed_s':a.clock()-task_start,'physical_stop_confirmed':False}
            if recovery_result['commands_sent']:result['no_motion']=False
        return result
    def hold():
        for key in tuple(sent_keys):
            try:
                a.transport.assert_exclusive(key);s=a._common()
                if key=='joint_command':
                    values={n:a._fresh(s['joints'].get(n),n)['position'] for p in moving if p in PARTS for n in PARTS[p] if n in public['targets']}
                    # A fresh exceptional reading is not a valid hold command.
                    # Do not ask the native driver to clamp it into a new pose.
                    if any(not a.profile['joint_limits_deg'][n][0]<=value<=a.profile['joint_limits_deg'][n][1] for n,value in values.items()):
                        raise ValueError('当前反馈越界，不能发送实测保持指令')
                    a.transport.publish(key,values)
                else:
                    side=key.split('_')[0];a.transport.publish(key,a._fresh(s['grippers'].get(side),side)['position'])
                held.append(key)
            except Exception:pass
    try:
        if cancel.is_set():return {'status':'cancelled','commands_sent':False}
        public,plan,waist,moving,keys=build(a,spec,a._common());last_parts=public['parts']
        acknowledge(public);task_deadline=execution_deadline(a,public,task_start)
        if public['no_motion']:return observe_no_motion(a,public,cancel,on_progress,guard_keys=source_keys(public),task_deadline=task_deadline)
        for key in keys:
            if cancel.is_set():return {'status':'cancelled','commands_sent':False,'parts':last_parts}
            a.transport.prepare(key);prepared.append(key)
        # Recheck the ENTIRE pose after endpoint preparation. No partial start.
        public,plan,waist,moving,keys=build(a,spec,a._common())
        acknowledge(public)
        if any(k not in prepared for k in keys):raise ValueError('准备期间状态改变，请重新检查整套动作')
        # Preparation may change valid measured origins and required duration.
        # Use this latest plan, never the first plan's shorter finish budget.
        task_deadline=execution_deadline(a,public,task_start)
        if public.get('head_boundary_recovery'):
            recovery_result=head_boundary_recovery.execute(a,public['head_boundary_recovery'],cancel,on_progress,deadline=task_deadline,guard_keys=source_keys(public))
            if recovery_result['cancelled']:
                return finish({'status':'cancelled','commands_sent':False,'hold_sent':False,'feedback_verified':False,'parts':last_parts})
            # Discard the entire provisional plan. No sample of an exceptional
            # trajectory may reach the ordinary publishers.
            public,plan,waist,moving,keys=build(a,spec,a._common())
            last_parts=public['parts']
            if public.get('head_boundary_recovery'):
                raise ValueError('头部确认后再次越界，本次取消后续动作；不会自动重试')
            if any(k not in prepared for k in keys):
                raise ValueError('回程期间其他部位状态改变，请重新检查整套动作')
            if a.clock()+public['motion_s']+.15>task_deadline:
                raise ValueError('回程后整套姿态所需时间超出本次任务预算')
            public['max_action_s']=min(public['max_action_s'],task_deadline-a.clock())
        if public['no_motion']:return finish(observe_no_motion(a,public,cancel,on_progress,guard_keys=source_keys(public),task_deadline=task_deadline,after_recovery=recovery_result is not None))
        start=a.clock();body=bool({'body','head'}&moving)
        cadence=BodyCadence(start,a.profile['body_command_hz'],a.profile['body_max_command_gap_s']) if body else None
        next_slow=start;arrival=None
        checkpoint_interval=public['checkpoint_budget_s']-.3
        next_checkpoint=checkpoint_interval;completed_checkpoints=0
        while not cancel.is_set():
            a._common()
            for key in source_keys(public):
                (a.transport.assert_exclusive_cached if cadence else a.transport.assert_exclusive)(key)
            snap=a._common();now=a.clock();elapsed=now-start
            if elapsed>public['max_action_s'] or now>task_deadline:raise ValueError('整套姿态未在预计时限内全部到位，已停止本次动作')
            items,last_parts=feedback(a,snap,public)
            if elapsed>=next_checkpoint:
                checkpoint_feedback(a,snap,plan,waist,elapsed,{p['id'] for p in public['parts']})
                completed_checkpoints+=1;next_checkpoint=(completed_checkpoints+1)*checkpoint_interval
            values=sample(plan,waist,elapsed)
            if cancel.is_set():break
            slow=now+1e-8>=next_slow
            if slow:next_slow=start+(math.floor((now-start)*a.profile['command_hz']+1e-8)+1)/a.profile['command_hz']
            joint={n:v for n,v in values.items() if not n.startswith('gripper_') and any(n in PARTS[p] for p in moving if p in PARTS) and (not n.startswith('arm_') or slow)}
            if cadence:cadence.dispatch(a.clock())
            if joint:sent_keys.add('joint_command');a.transport.publish('joint_command',joint)
            if slow:
                for side in public['grippers']:
                    if side+'_gripper' not in moving:continue
                    if cancel.is_set():break
                    key=side+'_gripper_command';sent_keys.add(key);a.transport.publish(key,values['gripper_'+side])
            done=elapsed>=plan.duration and all(p['status']=='at_target' for p in last_parts)
            if on_progress:on_progress({'elapsed_s':elapsed,'progress':min(1.,elapsed/plan.duration),'parts':last_parts,'feedback_arrived':done,'estimated_total_s':public['estimated_total_s']})
            if cancel.is_set():break
            if done:
                receipts={n:(x.get('receipt_seq'),now-x['age_s']) for n,x in items.items()}
                if any(seq is None for seq,_ in receipts.values()):raise ValueError('反馈缺少接收序号，无法确认整套到位')
                if arrival is None:arrival=receipts
                elif all(seq!=arrival[n][0] and at-arrival[n][1]>=.15 for n,(seq,at) in receipts.items()):
                    return finish({'status':'succeeded','commands_sent':bool(sent_keys),'feedback_verified':True,'parts':last_parts,'elapsed_s':elapsed,'checkpoint_checks':completed_checkpoints,'command_timing':cadence.report() if cadence else None,
                                   'arrival_feedback':arrival_feedback(a,public,items,now)})
            else:arrival=None
            cancel.wait(cadence.wait_s(a.clock()) if cadence else 1/a.profile['command_hz'])
        if sent_keys:hold()
        return finish({'status':'cancelled','commands_sent':bool(sent_keys),'hold_sent':bool(held),'feedback_verified':False,'parts':last_parts})
    except Exception as e:
        if sent_keys:hold()
        if recovery_result is not None:e.recovery=recovery_result
        e.commands_sent=bool(sent_keys) or bool(getattr(e,'commands_sent',False)) or bool(recovery_result and recovery_result['commands_sent'])
        e.parts=getattr(e,'parts',last_parts);e.hold_sent=bool(held)
        if hasattr(e,'recovery'):e.physical_stop_confirmed=False
        raise
    finally:
        for key in prepared:
            try:a.transport.release(key)
            except Exception:pass
        a._active_cancel=None;a._action_lock.release()
