from types import SimpleNamespace

import numpy as np

from f1_harness.aperture_view import ApertureViewpoint, planner_view_geometry


def pose(x=0, y=0, z=0):
    value = np.eye(4)
    value[:3, 3] = (x, y, z)
    return value


def setup():
    geometry = dict(object_id='block', hole_id='aperture', calibration_id='cal')
    config = dict(frame_chain={'frame': 'test'}, geometry=geometry)
    task = dict(geometry, allowed_skills=['move_tcp', 'reobserve'])
    footprint = dict(min=[-.03,-.03,-.03], max=[.03,.03,.03])
    plan = dict(task=task, fixture=dict(frame_chain=config['frame_chain'], cartesian_motion=True,
        eef_tip_reference=pose().tolist(), object_local_box=footprint, tool_local_boxes=[footprint],
        hole_inner_dimensions_m=[.04, .04], revalidation=dict(free_space_goal_tolerance_m=.015),
        obstacles=[dict(type='halfspace', role='table', normal=[0,0,1])]))
    tool = planner_view_geometry(plan,config)
    tracker = ApertureViewpoint(tool, SimpleNamespace(world_native=pose()), geometry)
    context = dict(owner_instance_id='owner', session_id='session', task_id='task', task_hash='hash',
                   task_generation=1, epoch=1, authorized=True, grasp_contact=dict(request_id='close'))
    return tracker, context, plan, config


def sample(name, x, *, z=0, aperture=False, held=True):
    object_pose = pose(x, 0, z).ravel().tolist()
    obj = SimpleNamespace(object_id='block', visible=True, pose=object_pose, held=held,
                          eef_object_pose=pose().ravel().tolist(), evidence_ids=['wr'])
    hole = SimpleNamespace(object_id='aperture', pose=pose().ravel().tolist(), visible=True, evidence_ids=['hr'])
    return SimpleNamespace(observation_id=name, acquisition_instance='acq', host_boot_id='boot',
                           calibration_id='cal', synthetic=False, objects=[obj,hole] if aperture else [obj],
                           frames=[SimpleNamespace(camera=c,blob_sha256=h) for c,h in [('head','hr'),('right','wr')]])


def test_view_uses_registered_envelopes_and_keeps_height_and_rotation():
    tracker, context, *_ = setup()
    tracker.update(sample('clear', -.4, aperture=True),context,pose(-.4))
    tracker.update(sample('blocked', 0, z=.2),context,pose(z=.2))
    move = tracker.move
    np.testing.assert_allclose(np.reshape(move['target_tcp_pose'], (4,4)), pose(-.05,0,.2))
    assert move['source_observation_id']=='clear' and move['current_observation_id']=='blocked'
    assert move['source_frame_hashes']==['hr']
    assert 'hole_pose' not in move


def test_changed_scope_regrasp_or_lost_attachment_cannot_reuse_view():
    for change in ('epoch','grasp_contact','held','visible','synthetic','unseen_hash','acquisition_instance'):
        tracker,context,*_ = setup()
        source = sample('clear', -.4, aperture=True)
        if change=='visible': source.objects[1].visible=False
        if change=='synthetic': source.synthetic=True
        if change=='unseen_hash': source.objects[1].evidence_ids=['not-in-this-exposure']
        tracker.update(source,context,pose(-.4))
        if change=='epoch':context['epoch']=2
        if change=='grasp_contact':context['grasp_contact']=dict(request_id='another-close')
        if change=='held':tracker.update(sample('released',0,held=False),context,pose())
        current=sample('blocked',0)
        if change=='acquisition_instance': current.acquisition_instance='new'
        tracker.update(current,context,pose())
        assert tracker.move is None, change


def test_view_needs_authorized_skills_and_same_tcp_frame_chain():
    _, _, plan, config = setup()
    plan['task']['allowed_skills'] = ['move_tcp']
    assert planner_view_geometry(plan,config) is None
    plan['task']['allowed_skills'].append('reobserve')
    config['frame_chain'] = {'frame':'different'}
    assert planner_view_geometry(plan,config) is None


def test_measured_oblique_head_can_recover_view_without_descending():
    tracker,context,*_ = setup()
    tracker.update(sample('clear', 0, aperture=True),context,pose())
    tracker.update(sample('blocked',0,z=.2),context,pose(z=.2),head_pose=pose(-1,0,1))
    goal=np.reshape(tracker.move['target_tcp_pose'],(4,4))
    np.testing.assert_allclose(goal,pose(-.05,0,.2))
