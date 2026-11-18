"""Production task runner against an explicitly local, actuator-free fixture."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from f1_harness.evidence import EvidenceStore

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--task',type=Path,required=True)
p.add_argument('--gateway',required=True)
p.add_argument('--image-gateway',required=True)
p.add_argument('--output',type=Path,required=True)
p.add_argument('--recorded-request',type=Path)
p.add_argument('--hint-receipt',type=Path,action='append',default=[])
a=p.parse_args()
for url in (a.gateway,a.image_gateway):
    if urlparse(url).hostname!='127.0.0.1':raise ValueError('local_fixture_only')
a.output.mkdir(parents=True,exist_ok=False)
store=EvidenceStore(a.output)
store.event('offline_contract_fixture',hardware_output=False,
    model='scripted' if a.recorded_request else 'actual',
    later_geometry='simulated',initial_geometry='saved real RGB-D/SAM')
if a.recorded_request:
    from f1_harness.contracts import MotionParameters
    request=json.loads(a.recorded_request.read_text())
    if request['name']!='approach_grasp':raise ValueError('recorded_approach_required')
    localize_program=request.get('program')=='localize_and_pick'
    candidates=request.get('candidates')
    if localize_program and candidates is not None:
        raise ValueError('localization_fixture_requires_one_explicit_approach')
    if candidates is not None:
        if not isinstance(candidates,list) or not 1<=len(candidates)<=6:
            raise ValueError('recorded_candidate_list_required')
        candidates=[MotionParameters.model_validate(v).for_skill('approach_grasp').supplied() for v in candidates]
        parameters=candidates[0]
    else:
        parameters=MotionParameters.model_validate(request['parameters']).for_skill('approach_grasp').supplied()
    import f1_harness.planner
    class ScriptedPlanner(f1_harness.planner.ResponsesPlanner):
        def __init__(self,*args,**kw):
            # Exercise the production tool loop without a client or credentials.
            self.store=store
            self.scripted_requests=0
        def prepare_connection(self):pass
        def close(self):pass
        def request(self,**kwargs):
            self.scripted_requests+=1
            if self.scripted_requests==1:
                name='python'
                program=('pick_with_candidates('+repr(candidates)+')' if candidates is not None
                         else 'pick_and_place(**'+repr(parameters)+')')
                if localize_program:
                    program='localize_and_pick('+repr(self.localization_hints)+', **'+repr(parameters)+')'
                arguments=dict(code='result = '+program+'\nprint(result)')
            else:
                name='finish'
                arguments=dict(status='stuck',summary='Saved program did not complete the owner task.')
            store.event('scripted_model_request',index=self.scripted_requests,api_calls=0,name=name)
            return dict(output=[dict(type='function_call',name=name,
                call_id='recorded-'+str(self.scripted_requests),arguments=json.dumps(arguments))])
        def run(self,arm,**kwargs):
            store.event('scripted_planner_program',api_calls=0,parameters=parameters,candidates=candidates)
            if not kwargs['continue_run']():raise RuntimeError('fixture_authority_ended')
            hint_calls=[];expected_images={}
            for receipt_path in a.hint_receipt:
                receipt=json.loads(receipt_path.read_text())
                expected_images.update(receipt['metadata'])
                responses=[json.loads(p.read_text())['content'] for p in receipt_path.parent.glob('llm-*-response.json')]
                response=next(r for r in responses if r.get('id')==receipt['response_id'])
                for call in response['output']:
                    if call.get('type')!='function_call':continue
                    if call['name']!='locate_entities':raise ValueError('unexpected_saved_localization_tool')
                    hint_calls.append(json.loads(call['arguments']))
            self.localization_hints={}
            for hint in hint_calls:
                self.localization_hints.setdefault(hint['camera'],[]).extend(hint['entities'])
            if hint_calls:
                for camera in dict.fromkeys(v['camera'] for v in hint_calls):
                    image=arm.execute_tool('python',dict(code=f'robo.show({camera!r})'))
                    if image.result.get('exception') or not image.images:raise ValueError('recorded_hint_image_failed')
                    actual=arm.backend.last_viewed_images[camera];expected=expected_images[camera]
                    if any(actual[k]!=expected[k] for k in
                            ('frame','observation_id','source_image_size','model_image_size','image_sha256')):
                        raise ValueError('saved_hint_image_identity_mismatch')
                if not localize_program:
                    code='\n'.join(f"robo.locate_entities({v['camera']!r}, {v['entities']!r})" for v in hint_calls)
                    result=arm.execute_tool('python',dict(code=code))
                    if result.result.get('exception'):raise ValueError('recorded_localization_failed')
                    store.event('scripted_actual_localization_calls',calls=hint_calls,result=result.result)
            if candidates is None and not localize_program:
                preview=arm.execute_tool('python',dict(code='initial_plan = robo.inspect_plan("approach_grasp", **'+repr(parameters)+')'))
                store.event('scripted_same_parameters_preview',result=preview.result)
                if preview.result.get('exception'):raise ValueError('recorded_candidate_preview_failed')
                if not any(c.get('tool')=='inspect_plan' and c.get('planned') is True
                           and c.get('current_scope') is True for c in preview.result.get('primitive_calls',[])):
                    raise ValueError('recorded_candidate_not_plannable')
            result=super().run(arm,**kwargs)
            store.event('scripted_program_result',result=result,scripted_requests=self.scripted_requests)
            return result
    f1_harness.planner.ResponsesPlanner=ScriptedPlanner
from f1_harness.task_runner import run_task
args=SimpleNamespace(task=a.task,gateway=a.gateway,image_gateway=a.image_gateway,
    authorization_source='web',start_timeout=10,planner='astra',experience_snapshot=None)
root=Path(__file__).resolve().parents[1]
try:
    result=run_task(args,store,root/'API.MD')
    store.write_json('result.json',dict(result=result,offline_fixture=True,physical_success=False))
    print(json.dumps(dict(outcome=result['measured']['outcome'],offline_fixture=True)))
except Exception as exc:
    store.write_json('error.json',dict(type=type(exc).__name__,reason=str(exc)[:500]))
    raise
