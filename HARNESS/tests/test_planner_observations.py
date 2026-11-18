import json
from copy import deepcopy
from types import SimpleNamespace
from f1_harness.planner import ResponsesPlanner
from f1_harness.evidence import EvidenceStore
from f1_harness.planner_observations import compact_history, deduplicate_feedback


def test_pixels_require_reason_and_are_sent_once(tmp_path):
    class Arm:
        def get_tools_spec(self):return [dict(name=n,description=n,input_schema={}) for n in ('python','finish')]
        def execute_tool(self,name,args):
            assert 'image_reason' not in args
            return SimpleNamespace(result={'status':'stuck'} if name=='finish' else {'stdout':'numeric state unavailable','primitive_calls':[dict(tool='observe',ok=True,scene_entities=[dict(object_id='square-1',visible=True,has_pose=True)])]},images=[] if name=='finish' else [b'\x89PNGfixture'])
    class Planner(ResponsesPlanner):
        def __init__(self):self.store=EvidenceStore(tmp_path);self.turn=0;self.initial_visual_needed=True
        def request(self,**kw):
            self.turn+=1;self.last_request_id=str(self.turn)*32
            images=[c for m in kw['input'] if isinstance(m.get('content'),list) for c in m['content'] if c.get('type')=='input_image']
            assert len(images)==(1 if self.turn==3 else 0)
            args={'code':'show'}
            if self.turn==2:args['image_reason']='No numeric aperture identity; need the head view to distinguish holes.'
            name='finish' if self.turn==4 else 'python'
            return {'output':[dict(type='function_call',name=name,call_id=str(self.turn),arguments=json.dumps(args if name=='python' else {}))]}
    p=Planner();progress=[]
    p.progress_callback=lambda state,**details:progress.append((state,details))
    assert p.run(Arm(),task_text='task')['status']=='stuck';assert p.turn==4
    assert [row[1]['round'] for row in progress if row[0]=='model']==[1,2,3,4]
    assert [row[1]['tool'] for row in progress if row[0]=='program']==['python']*3+['finish']


def test_history_preserves_unknown_execution_and_checkpoints():
    value={'primitive_calls':[dict(tool='execute_skill',request_id='r',outcome='pending_review',verification_request_id='v',reason='unknown_execution')], 'stdout':'large diagnostic'}
    inputs=[dict(type='function_call',call_id='a',name='python',arguments='{}'),dict(type='function_call_output',call_id='a',output=json.dumps(value)),dict(type='function_call_output',call_id='b',output='{"new":"state"}')]
    before=deepcopy(inputs);out=compact_history(inputs)
    assert inputs==before and out[0]==inputs[0] and out[-1]==inputs[-1]
    assert json.loads(out[1]['output'])['primitive_calls']==value['primitive_calls']


def test_exact_dedup_is_reconstructable_without_losing_facts():
    diagnostic={'code':'collision','numbers':list(range(300))}
    raw={'primitive_calls':[{'planning_diagnostic':diagnostic}], 'stdout':repr({'reason':'blocked','planning_diagnostic':diagnostic}), 'unknown':None}
    out=deduplicate_feedback(raw)
    assert out['printed_result']['reason']=='blocked' and out['unknown'] is None
    ref=out['printed_result']['planning_diagnostic']['same_as_json_pointer']
    assert ref=='/primitive_calls/0/planning_diagnostic'
    assert out['primitive_calls'][0]['planning_diagnostic']==diagnostic
    assert raw['stdout']==repr({'reason':'blocked','planning_diagnostic':diagnostic})


def test_history_projection_expands_references_before_removing_old_details():
    diagnostic={'reason':'missing','values':list(range(300))}
    value=deduplicate_feedback(dict(primitive_calls=[dict(tool='inspect_plan',planning_diagnostic=diagnostic)],decision_context=diagnostic))
    history=[dict(type='function_call_output',call_id='a',output=json.dumps(value)),dict(type='function_call_output',call_id='b',output='{}')]
    summary=json.loads(compact_history(history)[0]['output'])
    assert summary['decision_context']==diagnostic
    assert 'same_as_json_pointer' not in json.dumps(summary)


def test_old_printed_geometry_exits_context_but_latest_state_and_receipts_remain():
    old=dict(stdout='old numerical array '*2000,printed_result={'pose_history':list(range(3000))},
             primitive_calls=[dict(tool='execute_skill',request_id='r',outcome='pending_review',
                reason='unknown_publication',verification_request_id='r')])
    latest=dict(stdout='current geometry',primitive_calls=[dict(tool='observe',ok=True)])
    history=[dict(type='function_call_output',call_id='old',output=json.dumps(old)),
             dict(type='function_call_output',call_id='new',output=json.dumps(latest))]
    result=compact_history(history);summary=json.loads(result[0]['output'])
    assert 'stdout' not in summary and 'printed_result' not in summary
    assert summary['archived_output_fields']['stdout']['bytes']>10000
    assert summary['primitive_calls']==old['primitive_calls']
    assert json.loads(result[1]['output'])==latest
    assert len(result[0]['output'])<len(history[0]['output'])/10
    assert json.loads(history[0]['output'])==old
