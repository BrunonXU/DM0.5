"""Counterexamples for offline efficiency comparison; no robot or simulated plant required."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from f1_harness.contracts import digest
from f1_harness.planning_lessons import review_candidate_orders

spec=importlib.util.spec_from_file_location('order_comparison',Path(__file__).parents[1]/'scripts/compare_planning_order.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def write(p,value):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value))
def tracewrite(p,value):p.write_text('\n'.join(map(json.dumps,value)))


def make_report(root,name,offset,preferred=False):
    run=root/name;site=run/'site';tracepath=run/'collected/owner/harness/trace.jsonl'
    fixture=dict(frame_chain={'development_owner_config':str(site/'owner.local.json')},speed=20)
    if preferred:fixture['candidate_order']={'transfer_above_hole':[1,0,2,3]}
    write(site/'plan.local.json',{'fixture':fixture})
    for name_ in module.REVIEWED_FILES:
        if name_!='plan.local.json':write(site/name_,{})
    write(site/'development.local.json',dict(reviewed_files={n:module.sha(site/n) for n in module.REVIEWED_FILES}))
    scope=dict(skill='transfer_above_hole',task_hash='a'*64,calibration_id='cal',fixture_sha256=digest(fixture),runtime_source_sha256='b'*64)
    trace=[];intents={}
    for index,skill in enumerate(module.REQUIRED):
        request=f'{name}-{index}'
        intent=dict(request_id=request,owner_instance_id=name,session_id=name,task_hash='a'*64,epoch=1,
                    calibration_id='cal',skill=skill,excluded_candidates=[],candidate_start=0)
        intents[skill]=intent
        trace.append(dict(kind='skill_accepted',intent=intent))
        trace.append(dict(kind='skill_result',skill=skill,result=dict(request_id=request,outcome='success',synthetic=True,assisted=False,evidence_ids=['test-evidence'])))
    intent=intents['transfer_above_hole'];request=intent['request_id']
    attempts=[] if preferred else [dict(variant=0,outcome='rejected',elapsed_ns=800000000,diagnostic={'code':'scene_sweep_blocked'})]
    attempts.append(dict(variant=1,outcome='planned',elapsed_ns=1000000000))
    evaluation=dict(schema='f1-candidate-evaluation-v1',scope=scope,request_id=request,epoch=1,observation_id='obs',registered_variants=[0,1,2,3],
                    attempts=attempts,selected_variant=1,motion_success_inferred=False)
    proposal=dict(request_id=request,epoch=1,calibration_id='cal',candidate_variant=1,geometry_trace=[{'candidate_evaluation':evaluation}])
    trace.extend([dict(kind='harness_planning_started',request_id=request,intent=intent,observation={'observation_id':'obs'},simulation=True),
                  dict(kind='harness_plan_waiting_fresh_revalidation',event_id=name+'-event',request_id=request,candidate=proposal),
                  dict(kind='owner_placement_verified',request_id=intents['verify_placed']['request_id'],synthetic=True)])
    tracepath.parent.mkdir(parents=True);tracewrite(tracepath,trace)
    outputs=[dict(virtual_ns=i,submitted16=[0]*16,measured16=[0]*16,new_motion_reference=i==0,simulation=True,
                  output_evidence={'publication_complete':True}) for i in range(3)]
    write(run/'mock-owner-outputs.json',outputs)
    report=dict(schema='f1-full-owner-simulation-v1',mode='fake',physical_motion=False,robot_connected=False,all_results_synthetic=True,
        scenario=dict(site=str(site),follower='approved-output',scene_index=0,object_offset_mm=[offset,0,0],hole_offset_mm=[0,0,0],missing_scene_at=None,missing_scene_calls=0),
        source_scene_sha256='c'*64,error=None,result={'outcome':'success'},source_files={str(p):module.sha(p) for p in site.glob('*.json')},
        output_profile={'velocity_deg_s':30},computations=[dict(function='plan',skill='transfer_above_hole',wall_s=1. if preferred else 1.8)],virtual_s=23.6)
    p=run/'report.json';write(p,report);return p


@pytest.fixture
def case(tmp_path):
    discovery=make_report(tmp_path,'discovery',0)
    result=review_candidate_orders(discovery.parent,tmp_path/'memory')
    candidate=Path(module.read(result['path'])['candidates'][0]['path'])
    base=make_report(tmp_path,'baseline',2)
    trial=make_report(tmp_path,'trial',2,True)
    return candidate,base,trial


def test_matched_pair_is_diagnostic_not_a_promotion(case):
    result=module.compare(*case)
    assert result['comparison_valid'] and result['observed_planning_cost_improved']
    assert result['observed_saving_s']==pytest.approx(.8)
    assert result['motion_outputs_identical'] and result['synthetic'] and not result['publishable'] and not result['promoted']


def test_slower_trial_is_a_valid_comparison_but_not_an_improvement(case):
    candidate,base,trial=case;r=module.read(trial);r['computations'][0]['wall_s']=2.;write(trial,r)
    result=module.compare(*case)
    assert result['comparison_valid'] and not result['observed_planning_cost_improved']


@pytest.mark.parametrize('change,reason',[
    ('same_scene','independent_matched_geometry'),('mismatched_pair','independent_matched_geometry'),
    ('unfinished','incomplete_comparison'),('physical','full_owner_simulation'),('profile','output_profile_changed'),
    ('missing_reset','complete_unassisted_cycle'),('assisted','successful_bound_stage'),('cross_scope','cross_scope_task'),
    ('source_changed','comparison_source_changed'),('extra_config','trial_changes_more_than'),
    ('output_changed','comparison_motion_outputs_changed'),('no_order_use','preferred_order_not_applied'),
    ('missing_owner_verdict','owner_placement_evidence_missing')])
def test_misleading_speedup_cannot_pass(case,change,reason):
    candidate,base,trial=case;r=module.read(trial)
    tracepath=trial.parent/'collected/owner/harness/trace.jsonl';trace=module.rows(tracepath)
    if change=='same_scene':
        r['scenario']['object_offset_mm']=[0,0,0];b=module.read(base);b['scenario']['object_offset_mm']=[0,0,0];write(base,b)
    elif change=='mismatched_pair':r['scenario']['object_offset_mm']=[3,0,0]
    elif change=='unfinished':r['result']['outcome']='pending_review'
    elif change=='physical':r['physical_motion']=True
    elif change=='profile':r['output_profile']['velocity_deg_s']=60
    elif change=='missing_reset':trace=[t for t in trace if not (t['kind']=='skill_result' and t['skill']=='withdraw_reset')]
    elif change=='assisted':next(t for t in trace if t['kind']=='skill_result')['result']['assisted']=True
    elif change=='cross_scope':next(t for t in trace if t['kind']=='skill_accepted')['intent']['epoch']=2
    elif change in ('source_changed','extra_config'):
        cfgpath=Path(r['scenario']['site'])/'plan.local.json';cfg=module.read(cfgpath);cfg['fixture']['speed']=60;write(cfgpath,cfg)
        if change=='extra_config':
            r['source_files'][str(cfgpath)]=module.sha(cfgpath)
            next(t for t in trace if t['kind']=='harness_plan_waiting_fresh_revalidation')['candidate']['geometry_trace'][0]['candidate_evaluation']['scope']['fixture_sha256']=digest(cfg['fixture'])
    elif change=='output_changed':
        p=trial.parent/'mock-owner-outputs.json';v=module.read(p);v[1]['submitted16'][7]=2;write(p,v)
    elif change=='no_order_use':
        e=next(t for t in trace if t['kind']=='harness_plan_waiting_fresh_revalidation')['candidate']['geometry_trace'][0]['candidate_evaluation']
        e['attempts'].insert(0,dict(variant=0,outcome='rejected',elapsed_ns=2,diagnostic={'code':'scene_sweep_blocked'}))
    else:trace=[t for t in trace if t['kind']!='owner_placement_verified']
    write(trial,r);tracewrite(tracepath,trace)
    with pytest.raises(ValueError,match=reason):module.compare(*case)


def test_candidate_or_discovery_tamper_and_reuse_are_rejected(case):
    candidate,base,trial=case
    with pytest.raises(ValueError,match='independent_trace_required'):module.compare(candidate,base,base)
    value=module.read(candidate);value['candidate_order']=[1];write(candidate,value)
    with pytest.raises(ValueError,match='candidate_identity_changed'):module.compare(*case)


def test_cli_never_overwrites_existing_comparison(case,tmp_path,monkeypatch):
    output=tmp_path/'receipt.json'
    monkeypatch.setattr('sys.argv',['compare','--candidate',str(case[0]),'--baseline',str(case[1]),'--trial',str(case[2]),'--output',str(output)])
    module.main();before=output.read_bytes()
    with pytest.raises(FileExistsError):module.main()
    assert output.read_bytes()==before



@pytest.mark.parametrize('filename',['numerics.local.json','plan.local.json'])
def test_non_fixture_configuration_change_is_not_a_ranking_experiment(case,filename):
    candidate,base,trial=case;r=module.read(trial);site=Path(r['scenario']['site'])
    p=site/filename;config=module.read(p);config['different_solver']=True;write(p,config)
    admission=module.read(site/'development.local.json')
    admission['reviewed_files'][p.name]=module.sha(p);write(site/'development.local.json',admission)
    for p in site.glob('*.json'):r['source_files'][str(p)]=module.sha(p)
    write(trial,r)
    with pytest.raises(ValueError,match='non_planning_site_configuration_changed'):module.compare(*case)
