"""A padding exit must prove continuous separation, including rotating corners."""
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from f1_harness.geometry import tool_sweep_clear, tool_margin_departure_clear


def case():
    start=np.eye(4);start[0,3]=.014
    end=start.copy();end[0,3]=.02
    target=dict(role='target_object',object_id='block',min=[-.1,-.1,-.1],max=[0,.1,.1])
    boxes=[dict(min=[0,-.01,-.01],max=[.01,.01,.01])]
    return start,end,[target],dict(eef_tip_reference=np.eye(4),radius_m=.01,
        margin_m=.015,local_boxes=boxes,target_object_id='block')


def test_positive_separation_can_leave_padding_and_restore_full_margin():
    start,end,obstacles,kw=case()
    ordinary={k:v for k,v in kw.items() if k!='target_object_id'}
    assert not tool_sweep_clear(start,end,obstacles=obstacles,**ordinary)
    assert tool_margin_departure_clear(start,end,obstacles=obstacles,**kw)
    # Joint-coordinate frame choice must not change the physical decision.
    frame=np.eye(4);frame[:3,:3]=Rotation.from_euler('xyz',[.2,.4,-.6]).as_matrix()
    frame[:3,3]=[.2,-.3,.5]
    obstacles[0]['pose']=frame
    assert tool_margin_departure_clear(frame@start,frame@end,obstacles=obstacles,**kw)


@pytest.mark.parametrize('fault',['contact','overlap','toward','no_exit','rotation',
                                 'neighbor','wrong_id','table','other_box'])
def test_margin_departure_does_not_waive_physical_or_other_obstacle_checks(fault):
    start,end,obstacles,kw=case()
    if fault=='contact':start[0,3]=0
    elif fault=='overlap':start[0,3]=-.001
    elif fault=='toward':end[0,3]=.01
    elif fault=='no_exit':end[0,3]=.0145
    elif fault=='rotation':
        # Endpoint is clear, but a rotating corner can initially approach.
        end[:3,:3]=Rotation.from_euler('z',np.pi).as_matrix();end[0,3]=.04
    elif fault=='neighbor':obstacles[0]['role']='neighbor'
    elif fault=='wrong_id':kw['target_object_id']='another-block'
    elif fault=='table':obstacles.append(dict(type='halfspace',role='table',normal=[0,0,1],point=[0,0,0]))
    elif fault=='other_box':kw['local_boxes'].append(dict(min=[-.02,-.01,-.01],max=[0,.01,.01]))
    assert not tool_margin_departure_clear(start,end,obstacles=obstacles,**kw)


def test_measured_face_departure_prefixes_keep_one_reference_and_restore_margin():
    reference=np.eye(4);reference[2,3]=.01176
    goal=reference.copy();goal[2,3]+=.02
    plane=dict(type='halfspace',role='target_object',object_id='block',normal=[0,0,1],point=[0,0,0])
    kw=dict(eef_tip_reference=np.eye(4),radius_m=.03,obstacles=(plane,),margin_m=.015,
        local_boxes=[dict(min=[-.01,-.01,0],max=[.01,.01,.02])],target_object_id='block',
        departure_reference=reference,departure_goal=goal)
    prefix=reference.copy();prefix[2,3]=.013
    assert tool_margin_departure_clear(reference,prefix,**kw)
    assert tool_margin_departure_clear(prefix,goal,**kw)
    # Positive but decreasing clearance is still forbidden, including a plan
    # that retreats again later. The origin cannot be reset at each prefix.
    toward=prefix.copy();toward[2,3]=.0125
    assert not tool_margin_departure_clear(prefix,toward,**kw)
    below_origin=reference.copy();below_origin[2,3]=.011
    assert not tool_margin_departure_clear(below_origin,goal,**kw)
    overlap=reference.copy();overlap[2,3]=-.001
    assert not tool_margin_departure_clear(overlap,goal,**dict(kw,departure_reference=overlap))
    assert not tool_margin_departure_clear(reference,prefix,**dict(kw,departure_goal=prefix))
    swing=prefix.copy();swing[:3,:3]=Rotation.from_euler('y',np.pi).as_matrix()
    assert not tool_margin_departure_clear(reference,swing,**kw)
