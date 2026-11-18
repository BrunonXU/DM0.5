"""Evidence protocol tests; scripted owner journals never establish physical skill."""
import json
import time
from pathlib import Path

import pytest

from f1_harness.contracts import HarnessError, Task
from f1_harness.evidence import EvidenceStore
from f1_harness.memory import ExperienceStore, read_snapshot
from f1_harness.program_experience import (
    archive_run_programs, record_candidate, validated_agent_programs,
    register_run_agent_programs, review_agent_programs,
)
from f1_harness.sandbox import SandboxedArm


def proposal(goal):
    skill='verify_placed' if goal=='placed' else 'verify_grasp'
    return dict(title='Protocol test program',claim='Scripted evidence only',source=f'''def applicable(scene):
    return scene.get("available") is True

def run_skill(robo):
    result = robo.skill("grasp")
    if result.get("outcome") != "success":
        return result
    return robo.skill({skill!r})
''')


class JournalBackend:
    def __init__(self,run,*,synthetic=True,preflight_result=None):
        self.owner=EvidenceStore(run/'collected/owner/harness')
        self.synthetic=synthetic;self.steps=0;self.name=run.name
        self.preflight_result=preflight_result
        self.scope=dict(owner_instance_id=run.name+'-owner',session_id=run.name+'-session',
                        task_id='task',task_hash='task-hash',epoch=1,calibration_id='cal')
        self.owner.event('run_identity',scenario_id=run.name,scene_manifest_sha256=run.name+'-scene',
                         synthetic=synthetic,heldout=True)
        self.owner.event('task_prepared',task_hash='task-hash',
                         task=dict(shape='square',allowed_skills=['grasp','verify_grasp','verify_placed']))

    def execute_tool(self,name,input_dict):
        if name=='geometry_context':return dict(available=True)
        if name=='observe':return dict(observation=dict(observation_id='fresh',objects=[]),status={})
        assert name=='execute_skill'
        if self.preflight_result is not None:
            reply,self.preflight_result=self.preflight_result,None
            return reply
        self.steps+=1;request=f'{self.name}-request-{self.steps}';skill=input_dict['name']
        self.owner.event('skill_accepted',intent=dict(self.scope,request_id=request,skill=skill))
        if skill=='grasp':self.owner.event('harness_skill_first_publication',request_id=request,published_ns=time.monotonic_ns())
        result=dict(request_id=request,lifecycle='completed',outcome='success',reason='scripted_test',
                    evidence_ids=[request+'-evidence'],assisted=False,synthetic=self.synthetic,
                    no_motion=skill!='grasp')
        self.owner.event('skill_result',skill=skill,result=result)
        if skill=='verify_placed':self.owner.event('owner_placement_verified',request_id=request)
        return result

    def env_steps(self):return self.steps
    def solved(self):return False
    def cancel(self):pass


def create_run(run,*,goal='verify_grasp',synthetic=True,source=None,execute=True,preflight_result=None):
    backend=JournalBackend(run,synthetic=synthetic,preflight_result=preflight_result)
    store=EvidenceStore(run/'planner')
    candidate=source or proposal(goal)
    if execute:
        arm=SandboxedArm(backend,run/'worker').start()
        try:
            result,binding=arm.execute_candidate(candidate,store,call_id='call',request_id='model')
            assert not result.result.get('exception'),result.result
        finally:arm.close()
    record_candidate(store,candidate,call_id='call',request_id='model')
    root=run.parent/'archives'
    ids=archive_run_programs(run,root)
    return root/'program-candidates'/f'{ids[0]}.json'


def modify_rows(path, change):
    rows=[json.loads(x) for x in path.read_text().splitlines()]
    change(rows)
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))


@pytest.mark.parametrize('goal',['verify_grasp','placed'])
def test_actual_source_independent_goal_promotes_to_same_rpent_snapshot(tmp_path,goal):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    archive=create_run(tmp_path/'discovery',goal=goal)
    key=memory.propose_agent_program(archive,goal=goal)
    assert memory.snapshot()['count']==0
    validation=tmp_path/'heldout';create_run(validation,goal=goal)
    receipt=memory.verify(key,validation_run=validation,scenario_id='heldout')
    assert receipt['accepted'] and receipt['program_goal']==goal
    assert len(receipt['sequence_request_ids'])==2
    snapshot=read_snapshot(Path(memory.promote(key)['path']),synthetic=True)
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
    chosen=validated_agent_programs(snapshot,task,goal=goal,synthetic=True)
    assert len(chosen)==1 and chosen[0]['candidate']==proposal(goal)
    assert not validated_agent_programs(snapshot,task,goal=goal,synthetic=False)
    assert not validated_agent_programs(snapshot,task.model_copy(update={'calibration_id':'other'}),
                                       goal=goal,synthetic=True)
    assert not validated_agent_programs(snapshot,task.model_copy(update={'allowed_skills':('reobserve',)}),
                                       goal=goal,synthetic=True)


@pytest.mark.parametrize('mutation',['assisted','missing_evidence','other_epoch','other_calibration',
    'other_shape','revoked','unrelated_owner_action','no_publication','changed_source',
    'missing_host_start','changed_rpc_blob','mode_mismatch'])
def test_unrelated_or_unbound_success_never_validates_python(tmp_path,mutation):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    key=memory.propose_agent_program(create_run(tmp_path/'discovery'),goal='verify_grasp')
    run=tmp_path/'heldout'
    alternate=proposal('verify_grasp')
    alternate['source']+='\n\ndef extra():\n    return 1\n'
    create_run(run,source=alternate if mutation=='changed_source' else None)
    owner=run/'collected/owner/harness/trace.jsonl'
    def change(rows):
        accepted=next(r for r in rows if r.get('kind')=='skill_accepted' and r['intent']['skill']=='verify_grasp')
        outcome=next(r for r in rows if r.get('kind')=='skill_result' and r['skill']=='verify_grasp')
        if mutation=='assisted':outcome['result']['assisted']=True
        if mutation=='missing_evidence':outcome['result']['evidence_ids']=[]
        if mutation=='other_epoch':accepted['intent']['epoch']=2
        if mutation=='other_calibration':accepted['intent']['calibration_id']='other'
        if mutation=='other_shape':next(r for r in rows if r['kind']=='task_prepared')['task']['shape']='circle'
        if mutation=='revoked':rows.insert(rows.index(accepted),dict(kind='authority_revoked'))
        if mutation=='unrelated_owner_action':rows.insert(rows.index(accepted),dict(kind='skill_accepted',
            intent=dict(accepted['intent'],request_id='unrelated')))
        if mutation=='no_publication':rows[:]=[r for r in rows if r['kind']!='harness_skill_first_publication']
        if mutation=='mode_mismatch':rows[0]['synthetic']=False
    modify_rows(owner,change)
    if mutation=='missing_host_start':
        modify_rows(run/'planner/trace.jsonl',lambda rows:rows.__setitem__(slice(None),
            [r for r in rows if r['kind']!='program_execution_started']))
    if mutation=='changed_rpc_blob':
        rows=[json.loads(x) for x in (run/'planner/trace.jsonl').read_text().splitlines()]
        row=next(r for r in rows if r['kind']=='program_backend_result')
        (run/'planner/blobs'/row['result_sha256']).write_text('{}')
    receipt=memory.verify(key,validation_run=run,scenario_id='heldout')
    assert not receipt['accepted']
    with pytest.raises(HarnessError,match='not_verified'):memory.promote(key)
    assert memory.snapshot()['count']==0


@pytest.mark.parametrize('same',['run','scene','scenario'])
def test_independent_run_and_scene_required(tmp_path,same):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    discovery=tmp_path/'discovery'
    key=memory.propose_agent_program(create_run(discovery),goal='verify_grasp')
    run=discovery if same=='run' else tmp_path/'heldout'
    if same!='run':create_run(run)
    if same=='scene':modify_rows(run/'collected/owner/harness/trace.jsonl',
        lambda rows:rows[0].update(scene_manifest_sha256='discovery-scene'))
    with pytest.raises(HarnessError,match='independent'):
        memory.verify(key,validation_run=run,scenario_id='discovery' if same in ('run','scenario') else 'heldout')


def test_proposal_alone_is_not_registered_execution(tmp_path):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    archive=create_run(tmp_path/'discovery',execute=False)
    with pytest.raises(HarnessError,match='execution_unbound'):
        memory.propose_agent_program(archive,goal='verify_grasp')


def test_changed_host_trace_after_review_cannot_promote(tmp_path):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    key=memory.propose_agent_program(create_run(tmp_path/'discovery'),goal='verify_grasp')
    run=tmp_path/'heldout';create_run(run)
    assert memory.verify(key,validation_run=run,scenario_id='heldout')['accepted']
    EvidenceStore(run/'planner').event('unexpected_later_edit')
    with pytest.raises(HarnessError,match='program_validation_changed'):memory.promote(key)


def test_held_object_success_outside_invocation_does_not_validate(tmp_path):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    key=memory.propose_agent_program(create_run(tmp_path/'discovery'),goal='verify_grasp')
    run=tmp_path/'heldout';create_run(run)
    # Move the goal result to an unrelated request, retaining the successful
    # owner verdict. The program's actual RPC receipt still names its own ID.
    def change(rows):
        for row in rows:
            if row.get('kind')=='skill_result' and row.get('skill')=='verify_grasp':
                row['result']['request_id']='outside-program'
    modify_rows(run/'collected/owner/harness/trace.jsonl',change)
    assert not memory.verify(key,validation_run=run,scenario_id='heldout')['accepted']


def test_real_mode_requires_heldout_owner_identity(tmp_path):
    # These are constructed protocol fixtures, not real robot evidence.
    memory=ExperienceStore(tmp_path/'memory',synthetic=False)
    key=memory.propose_agent_program(create_run(tmp_path/'discovery',synthetic=False),goal='verify_grasp')
    run=tmp_path/'heldout';create_run(run,synthetic=False)
    modify_rows(run/'collected/owner/harness/trace.jsonl',lambda rows:rows[0].update(heldout=False))
    assert not memory.verify(key,validation_run=run,scenario_id='heldout')['accepted']


def test_renamed_run_cannot_reuse_discovery_owner_session(tmp_path):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    key=memory.propose_agent_program(create_run(tmp_path/'discovery'),goal='verify_grasp')
    run=tmp_path/'heldout';create_run(run)
    def change(rows):
        for row in rows:
            if row.get('kind')=='skill_accepted':
                row['intent'].update(owner_instance_id='discovery-owner',session_id='discovery-session')
    modify_rows(run/'collected/owner/harness/trace.jsonl',change)
    with pytest.raises(HarnessError,match='independent'):
        memory.verify(key,validation_run=run,scenario_id='heldout')


def test_sealed_run_registers_measured_goal_then_explicit_review_promotes(tmp_path):
    discovery=tmp_path/'discovery';root=tmp_path/'memory'
    create_run(discovery)
    registration=register_run_agent_programs(discovery,root)
    assert len(registration['candidate_ids'])==1 and registration['promoted'] is False
    key=registration['candidate_ids'][0]
    memory=ExperienceStore(root,synthetic=True)
    assert memory._candidate(key)['program_goal']=='verify_grasp'
    assert memory.snapshot()['count']==0
    run=tmp_path/'heldout';create_run(run)
    result=review_agent_programs(run,root,'heldout')
    assert result['promoted']==[key] and result['snapshot']['count']==1


def test_missing_scene_identity_stays_archived_without_invented_heldout(tmp_path):
    run=tmp_path/'discovery';root=tmp_path/'memory';create_run(run)
    owner=run/'collected/owner/harness/trace.jsonl'
    modify_rows(owner,lambda rows:rows[0].update(scene_manifest_sha256=None,heldout=False))
    before=owner.read_bytes()
    registration=register_run_agent_programs(run,root)
    assert registration['archive_ids'] and not registration['candidate_ids']
    review=review_agent_programs(run,root,'discovery')
    assert review['reason']=='heldout_owner_scene_identity_required' and not review['promoted']
    assert owner.read_bytes()==before


@pytest.mark.parametrize('tail',['robo.skill("release")','robo.skill("reobserve")',
                                'raise ValueError("failure after observed grasp")'])
def test_intermediate_grasp_success_cannot_register_terminal_goal(tmp_path,tail):
    candidate=proposal('verify_grasp')
    candidate['source']=candidate['source'].replace("return robo.skill('verify_grasp')",
        "robo.skill('verify_grasp')\n    "+tail)
    run=tmp_path/'discovery'
    # The exception case intentionally preserves the owner receipt but leaves
    # the host invocation incomplete; create_run normally asserts no exception.
    backend=JournalBackend(run);store=EvidenceStore(run/'planner')
    arm=SandboxedArm(backend,run/'worker').start()
    try:arm.execute_candidate(candidate,store,call_id='call',request_id='model')
    finally:arm.close()
    record_candidate(store,candidate,call_id='call',request_id='model')
    result=register_run_agent_programs(run,tmp_path/'memory')
    assert result['archive_ids'] and not result['candidate_ids']


def test_failed_independent_trial_does_not_permanently_veto_later_success(tmp_path):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    key=memory.propose_agent_program(create_run(tmp_path/'discovery'),goal='verify_grasp')
    failed=tmp_path/'failed';create_run(failed)
    modify_rows(failed/'collected/owner/harness/trace.jsonl',lambda rows:rows.__setitem__(slice(None),
        [r for r in rows if r['kind']!='harness_skill_first_publication']))
    assert not memory.verify(key,validation_run=failed,scenario_id='failed')['accepted']
    with pytest.raises(HarnessError,match='not_verified'):memory.promote(key)
    passed=tmp_path/'passed';create_run(passed)
    assert memory.verify(key,validation_run=passed,scenario_id='passed')['accepted']
    assert memory.promote(key)['count']==1
    assert len(list(memory.records.root.glob(key+'.review-*.json')))==2


@pytest.mark.parametrize('no_motion',[True,False,None])
def test_only_proven_unsubmitted_rejection_may_precede_successful_program(tmp_path,no_motion):
    memory=ExperienceStore(tmp_path/'memory',synthetic=True)
    source=proposal('verify_grasp')
    source['source']=source['source'].replace('    result = robo.skill("grasp")',
        '    robo.skill("reobserve")\n    result = robo.skill("grasp")')
    key=memory.propose_agent_program(create_run(tmp_path/'discovery',source=source),goal='verify_grasp')
    run=tmp_path/'heldout'
    create_run(run,source=source,preflight_result=dict(request_id='not-submitted',
        lifecycle='rejected',outcome='pending_review',no_motion=no_motion,reason='missing_geometry'))
    receipt=memory.verify(key,validation_run=run,scenario_id='heldout')
    assert receipt['accepted'] is (no_motion is True)
    assert 'not-submitted' not in receipt['sequence_request_ids']


def test_run_summary_promotes_only_explicit_owner_heldout_program_validation(tmp_path):
    from f1_harness.run_review import summarize_run
    root=tmp_path/'memory';discovery=tmp_path/'discovery'
    create_run(discovery)
    modify_rows(discovery/'collected/owner/harness/trace.jsonl',lambda rows:rows[0].update(heldout=False))
    found=summarize_run(discovery,root)
    key=found['agent_programs']['candidate_ids'][0]
    assert 'independent_validation' not in found['agent_programs']
    assert ExperienceStore(root,synthetic=True).snapshot()['count']==0
    validation=tmp_path/'heldout';create_run(validation)
    owner_before=(validation/'collected/owner/harness/trace.jsonl').read_bytes()
    reviewed=summarize_run(validation,root)
    assert key in reviewed['agent_programs']['independent_validation']['promoted']
    assert ExperienceStore(root,synthetic=True).snapshot()['count']==1
    assert (validation/'collected/owner/harness/trace.jsonl').read_bytes()==owner_before
    # A repeated summary still uses the already sealed host trace.
    again=summarize_run(validation,root)
    assert key in again['agent_programs']['independent_validation']['promoted']


def test_run_summary_does_not_upgrade_missing_validation_identity(tmp_path):
    from f1_harness.run_review import summarize_run
    run=tmp_path/'discovery';create_run(run)
    owner=run/'collected/owner/harness/trace.jsonl'
    modify_rows(owner,lambda rows:rows[0].update(scene_manifest_sha256=None,heldout=False))
    original=owner.read_bytes()
    summary=summarize_run(run,tmp_path/'memory')
    assert summary['agent_programs']['archive_ids'] and not summary['agent_programs']['candidate_ids']
    assert 'independent_validation' not in summary['agent_programs']
    assert owner.read_bytes()==original
