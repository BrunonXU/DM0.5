"""Read-only whole-task diagnostics; review never prepares command outputs.

The exact same complete planner is used for review and actual execution. Long
valid paths remain a single executable task, with internal feedback checkpoints.
Exceptional origins, limits, ownership and geometry are never bypassed.
"""
from .pose_action import build, PARTS, LABELS
from .adapter import ActionRejected


def issue(error, part, a, snap):
    row={'part':part,'label':LABELS.get(part,'整套姿态'),'code':'blocked','message':str(error)}
    if hasattr(error,'motion_s'):
        row.update(code='duration',motion_s=error.motion_s)
    prefix='当前反馈超出已核验限位: '
    if str(error).startswith(prefix):
        name=str(error)[len(prefix):];value=snap['joints'].get(name,{}).get('position');limits=a.profile['joint_limits_deg'].get(name)
        row.update(code='origin_out_of_range',joint=name,current=value,limits=limits)
        row['message']=f'{name} 实测 {value:.2f}°，超出配置范围 {limits[0]:g}°～{limits[1]:g}°。驱动会截断越界命令，需先核对该关节的标定与原生恢复方式。'
    return row


def review(a, spec):
    snap=a._common()
    try:
        public=build(a,spec,snap)[0]
        return {'executable':True,'plan':public,'issues':[],'segments':[]}
    except (ValueError,ActionRejected) as e:
        first=e
    targets=spec.get('targets',{});grips=spec.get('grippers',{})
    if not isinstance(targets,dict) or not isinstance(grips,dict):
        return {'executable':False,'issues':[issue(first,'pose',a,snap)],'segments':[]}
    issues=[]
    # Inspect every selected part even when another part is invalid.
    for part,names in PARTS.items():
        selected={n:v for n,v in targets.items() if n in names}
        child={'kind':'pose','targets':selected}
        if part=='body' and 'waist_delta_m' in spec:child['waist_delta_m']=spec['waist_delta_m']
        if not selected and 'waist_delta_m' not in child:continue
        try:build(a,child,snap)
        except (ValueError,ActionRejected) as e:issues.append(issue(e,part,a,snap))
    for side,value in grips.items():
        try:build(a,{'kind':'pose','grippers':{side:value}},snap)
        except (ValueError,ActionRejected) as e:issues.append(issue(e,side+'_gripper',a,snap))
    if not issues or (not hasattr(first,'motion_s') and not any(str(first)==r['message'] or (r.get('joint') and str(first).endswith(r['joint'])) for r in issues)):
        issues.append(issue(first,'pose',a,snap))
    report={'executable':False,'issues':issues,'segments':[]}
    # Never silently discard an invalid part or offer an incomplete reset.
    return report
