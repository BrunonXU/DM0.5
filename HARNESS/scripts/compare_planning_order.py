"""Independent offline A/B check for a planning-order proposal; never publishes it.

This tool accepts the full-owner simulator's evidence, not physical runs. Real
policy promotion must separately validate held-out hardware outcomes. It does
not execute model text, connect devices, or alter a runtime configuration.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from f1_harness.contracts import digest
from f1_harness.planning_lessons import validate_evaluation
from f1_harness.development import REVIEWED_FILES

PROJECT=Path(__file__).resolve().parents[2]
REQUIRED=['approach_grasp','grasp','verify_grasp','inspect_in_hand','transfer_above_hole',
          'align','insert','release','verify_placed','withdraw_reset']


def require(condition,reason):
    if not condition:raise ValueError(reason)


def path(value):
    p=Path(value)
    return p if p.is_absolute() else PROJECT/p


def read(p):return json.loads(Path(p).read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def rows(p):return [json.loads(line) for line in Path(p).read_text().splitlines() if line.strip()]


def evaluation(trace,skill):
    starts=[r for r in trace if r.get('kind')=='harness_planning_started' and r['intent']['skill']==skill]
    require(len(starts)==1,'one_planning_attempt_scope_required')
    start=starts[0];request=start['request_id']
    plans=[r for r in trace if r.get('kind')=='harness_plan_waiting_fresh_revalidation' and r['request_id']==request]
    require(len(plans)==1,'one_planned_candidate_required')
    candidate=plans[0]['candidate'];value=candidate['geometry_trace'][0]['candidate_evaluation']
    validate_evaluation(value,candidate,start)
    require(not start['intent']['excluded_candidates'] and start['intent']['candidate_start']==0,
            'comparison_with_excluded_candidates_unsupported')
    return value


def fixture(report):
    site=path(report['scenario']['site']).resolve()
    value=read(site/'plan.local.json')['fixture']
    if value['frame_chain'].get('development_owner_config'):
        value['frame_chain']['development_owner_config']=str(site/'owner.local.json')
    return value


def comparable_fixture(value):
    value=deepcopy(value)
    if value['frame_chain'].get('development_owner_config'):
        value['frame_chain']['development_owner_config']='OFFLINE_SITE/owner.local.json'
    return value


def site_configuration(report):
    site=path(report['scenario']['site']).resolve()
    values={p.name:read(p) for p in site.glob('*.json')}
    require(set(REVIEWED_FILES)<=set(values) and 'development.local.json' in values,
            'complete_reviewed_site_required')
    reviewed=values['development.local.json']['reviewed_files']
    for name in REVIEWED_FILES:
        expected=sha(site/name)
        require(reviewed.get(name)==expected and report['source_files'].get(str(site/name))==expected,
                'reviewed_site_identity_changed:'+name)
    # The only permissible declaration change is the digest of the new order.
    values['development.local.json']['reviewed_files'].pop('plan.local.json')
    # Fixture equality is checked separately, allowing only candidate_order.
    # Keep the planner's other fields (backend, timing, solver configuration).
    values['plan.local.json'].pop('fixture')
    return values


def geometry_key(report):
    s=report['scenario']
    require(s['missing_scene_at'] is None and s['missing_scene_calls']==0,'fault_injection_not_timing_comparison')
    return dict(scene=report['source_scene_sha256'],index=s['scene_index'],
                object_offset=s['object_offset_mm'],hole_offset=s['hole_offset_mm'])


def inspect_report(p,skill):
    p=Path(p).resolve();r=read(p)
    require(r['schema']=='f1-full-owner-simulation-v1' and r['mode']=='fake'
            and r['physical_motion'] is False and r['robot_connected'] is False
            and r['all_results_synthetic'] is True and r['scenario']['follower']=='approved-output',
            'full_owner_simulation_only')
    require(r.get('error') is None and r['result']['outcome']=='success','incomplete_comparison_task')
    for source,expected in r['source_files'].items():
        require(sha(source)==expected,'comparison_source_changed:'+source)
    trace_path=p.parent/'collected/owner/harness/trace.jsonl';trace=rows(trace_path)
    accepted={v['intent']['request_id']:v['intent'] for v in trace if v.get('kind')=='skill_accepted'}
    results=[v for v in trace if v.get('kind')=='skill_result']
    require([v['skill'] for v in results]==REQUIRED,'complete_unassisted_cycle_required')
    scopes=[]
    for row in results:
        result=row['result'];intent=accepted[result['request_id']]
        require(intent['skill']==row['skill'] and result['outcome']=='success' and result['synthetic'] is True
                and result['assisted'] is False and bool(result['evidence_ids']),'successful_bound_stage_required')
        scopes.append(tuple(intent[k] for k in ('owner_instance_id','session_id','task_hash','epoch')))
    require(len(set(scopes))==1,'cross_scope_task_results')
    placed=next(v['result']['request_id'] for v in results if v['skill']=='verify_placed')
    require(any(v.get('kind')=='owner_placement_verified' and v['request_id']==placed and v['synthetic'] is True for v in trace),
            'owner_placement_evidence_missing')
    output_path=p.parent/'mock-owner-outputs.json';outputs=read(output_path)
    require(outputs and all(o['simulation'] is True and o['output_evidence']['publication_complete'] for o in outputs),
            'simulation_output_incomplete')
    ev=evaluation(trace,skill);effective=fixture(r)
    require(digest(effective)==ev['scope']['fixture_sha256'],'evaluation_fixture_binding_changed')
    computations=[c['wall_s'] for c in r['computations'] if c['function']=='plan' and c['skill']==skill]
    require(len(computations)==1 and computations[0]>0,'one_measured_plan_cost_required')
    return dict(path=p,report=r,trace=trace,evaluation=ev,fixture=effective,plan_s=computations[0],
                outputs=outputs,trace_path=trace_path,output_path=output_path)


def compare(candidate_path,baseline_path,trial_path):
    candidate_path=Path(candidate_path).resolve();candidate=read(candidate_path)
    require(candidate_path.name==digest(candidate)+'.candidate.json','candidate_identity_changed')
    require(candidate['schema']=='f1-planning-order-candidate-v1' and candidate['synthetic'] is True
            and candidate['execution_enabled'] is False and candidate['operation']=='prefer_registered_candidates'
            and candidate['limits_modified'] is False,'quarantined_simulation_order_required')
    skill=candidate['scope']['skill'];discovery_path=path(candidate['source_run'])/'report.json'
    discovery=inspect_report(discovery_path,skill)
    require(sha(discovery['trace_path'])==candidate['source_sha256']
            and discovery['evaluation']==candidate['evaluation'],'discovery_evidence_changed')
    selected=candidate['evaluation']['selected_variant'];registered=candidate['retained_variants']
    require(candidate['candidate_order']==[selected]+[v for v in registered if v!=selected]
            and registered==candidate['evaluation']['registered_variants'],'candidate_drops_or_changes_variants')
    base=inspect_report(baseline_path,skill);trial=inspect_report(trial_path,skill)
    require(len({sha(v['trace_path']) for v in (discovery,base,trial)})==3,'independent_trace_required')
    require(geometry_key(base['report'])==geometry_key(trial['report'])
            and geometry_key(base['report'])!=geometry_key(discovery['report']),'independent_matched_geometry_required')
    require(base['report']['output_profile']==trial['report']['output_profile']==discovery['report']['output_profile'],
            'output_profile_changed')
    for value in (base,trial):
        for key in ('task_hash','calibration_id','skill','runtime_source_sha256'):
            require(value['evaluation']['scope'][key]==candidate['scope'][key],'comparison_scope_changed:'+key)
    original=comparable_fixture(discovery['fixture']);expected=deepcopy(original)
    expected.setdefault('candidate_order',{})[skill]=candidate['candidate_order']
    require(comparable_fixture(base['fixture'])==original,'baseline_fixture_changed')
    require(comparable_fixture(trial['fixture'])==expected,'trial_changes_more_than_candidate_order')
    require(site_configuration(base['report'])==site_configuration(trial['report'])
            ==site_configuration(discovery['report']),'non_planning_site_configuration_changed')
    require(trial['evaluation']['attempts'][0]['variant']==selected,'preferred_order_not_applied')
    require(trial['evaluation']['selected_variant']==base['evaluation']['selected_variant'],
            'different_selected_motion_candidate')
    def output_values(v):
        return [(o['virtual_ns'],o['submitted16'],o['measured16'],o['new_motion_reference']) for o in v['outputs']]
    require(output_values(base)==output_values(trial),'comparison_motion_outputs_changed')
    saving=base['plan_s']-trial['plan_s']
    files=[candidate_path,Path(__file__)]
    for value in (discovery,base,trial):files.extend([value['path'],value['trace_path'],value['output_path']])
    return dict(schema='f1-planning-order-offline-comparison-v1',candidate_id=digest(candidate),
        comparison_valid=True,observed_planning_cost_improved=saving>0,
        baseline_plan_s=base['plan_s'],trial_plan_s=trial['plan_s'],observed_saving_s=saving,
        baseline_virtual_s=base['report']['virtual_s'],trial_virtual_s=trial['report']['virtual_s'],
        matched_simulated_geometry=geometry_key(trial['report']),motion_outputs_identical=True,
        source_sha256={str(p):sha(p) for p in files},synthetic=True,publishable=False,promoted=False,
        limitations=['Independent synthetic geometry, not independent real-world data.',
            'Single paired timing observation, not a statistical speedup or physical-cycle measurement.',
            'Source/evidence integrity and identical simulated outputs do not certify physical perception or contact.',
            'No automatic policy publication; real held-out outcome and timing validation remain required.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('candidate','baseline','trial','output'):parser.add_argument('--'+name,required=True,type=Path)
    args=parser.parse_args();result=compare(args.candidate,args.baseline,args.trial)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as stream:json.dump(result,stream,indent=2,allow_nan=False);stream.write('\n')
    print(json.dumps({key:result[key] for key in ('comparison_valid','observed_saving_s','publishable')}))


if __name__=='__main__':main()
