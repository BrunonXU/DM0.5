"""External VLA grasp is measured evidence, never fabricated skill completion."""
import pytest
from f1_harness.contracts import SceneObject,SkillIntent,digest,HarnessError
from test_unpublished_plan import make_bridge


@pytest.mark.parametrize('skill',['transfer_above_hole','inspect_in_hand'])
@pytest.mark.parametrize('held,evidence,scope,local,allowed',[
 (True,True,True,True,True),(None,True,True,True,False),
 (False,True,True,True,False),(True,False,True,True,False),
 (True,True,False,True,False),(True,True,True,False,False)])
def test_measured_attachment_and_local_scope_required(tmp_path,skill,held,evidence,scope,local,allowed):
    e,b,clock,_,_,obs=make_bridge(tmp_path)
    try:
        e.pending=None;e.results.clear()
        e.task=e.task.model_copy(update={'allowed_skills':(skill,)})
        e.hybrid_skill_allowed=lambda:local
        e.hybrid_attachment_scope=(e.instance,e.session,e.epoch if scope else e.epoch-1)
        pose=(1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.)
        obj=SceneObject(object_id='block',shape='square',held=held,eef_object_pose=pose,
            pose=pose,frame_id='native',visible=True,evidence_ids=('image',) if evidence else ())
        e.update_observation(obs.model_copy(update={'objects':(obj,)}))
        intent=SkillIntent(request_id='tail',session_id=e.session,owner_instance_id=e.instance,
            task_id=e.task.task_id,task_hash=digest(e.task),epoch=e.epoch,
            observation_id=obs.observation_id,scene_version=obs.scene_version,
            calibration_id=e.task.calibration_id,skill=skill)
        if allowed:
            e.submit_skill(intent.model_dump());assert e.pending.skill==skill
            assert not e.completed_skills
        else:
            result=e.submit_skill(intent.model_dump())
            assert result['lifecycle']=='rejected' and result['no_motion'] is True
            assert 'skill_precondition' in result['reason'] or 'hybrid_local_handoff_required' in result['reason']
            assert e.pending is None
    finally:b.close()

@pytest.mark.parametrize('skill,held,closed,released,scope,allowed',[
 ('verify_grasp',None,True,False,True,True),
 ('verify_grasp',None,True,False,False,False),
 ('recover_grasp',None,True,False,True,True),
 ('regrasp',False,True,False,True,True),
 ('regrasp',None,True,False,True,False),
 ('verify_placed',False,False,True,True,True),
 ('verify_placed',False,False,False,True,False),
 ('approach_grasp',None,True,False,True,False),
 ('approach_grasp',True,True,False,True,False),
 ('approach_grasp',False,False,False,True,True),
])
def test_external_execution_enters_existing_verifiers_without_fake_skills(tmp_path,skill,held,closed,released,scope,allowed):
    e,b,clock,_,_,obs=make_bridge(tmp_path)
    try:
        e.pending=None;e.results.clear()
        e.task=e.task.model_copy(update={'allowed_skills':(skill,), 'regrasp_limit':1})
        e.external_execution=dict(scope=(e.instance,e.session,e.epoch if scope else e.epoch-1),
            task_hash=digest(e.task),closed=closed,released=released)
        if skill=='recover_grasp':e.attempted_motion.add('verify_grasp')
        obj=SceneObject(object_id='block',shape='square',held=held,stationary=True,
            visible=True,evidence_ids=('image',))
        e.update_observation(obs.model_copy(update={'objects':(obj,)}))
        intent=SkillIntent(request_id='external',session_id=e.session,owner_instance_id=e.instance,
            task_id=e.task.task_id,task_hash=digest(e.task),epoch=e.epoch,
            observation_id=obs.observation_id,scene_version=obs.scene_version,
            calibration_id=e.task.calibration_id,skill=skill)
        result=e.submit_skill(intent.model_dump())
        assert (result['lifecycle']=='accepted') is allowed,result
        assert not e.completed_skills
        if not allowed:assert result['no_motion'] is True
    finally:b.close()
