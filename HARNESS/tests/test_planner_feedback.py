from copy import deepcopy
from f1_harness.planner_feedback import compact_feedback


def test_exact_diagnostic_echo_removed_but_raw_result_and_ledger_preserved():
    diagnostic=dict(planned=False,current_scope=True,planning_diagnostic=dict(code='collision',metrics={'x':.1}))
    raw=dict(stdout=repr(dict(stage='preview',diagnostics=[diagnostic]))+'\n',
             primitive_calls=[dict(tool='preview_skill',**diagnostic)])
    before=deepcopy(raw);out=compact_feedback(raw)
    assert raw==before and out['primitive_calls']==raw['primitive_calls']
    assert out['stdout_receipts_deduplicated']==1 and 'preview' in out['stdout']
    assert len(out['stdout'])<len(raw['stdout'])


def test_different_geometry_and_truncated_stdout_are_not_hidden():
    receipt=dict(planned=False,current_scope=True)
    for printed in [dict(receipt,extra_geometry=[1,2,3]),dict(receipt,planned=True),{'calculation':42}]:
        raw=dict(stdout=repr(printed)+'\n',primitive_calls=[receipt])
        assert compact_feedback(raw)==raw
    raw=dict(stdout="{'planned': False, 'current_scope': Truncated",primitive_calls=[receipt])
    assert compact_feedback(raw)==raw


def test_checkpoint_error_and_unrelated_prose_survive_deduplication():
    receipt=dict(request_id='a',outcome='pending_review',verification_request_id='a')
    raw=dict(stdout=repr(receipt)+'\nneed changed viewpoint\n',exception='original exception',
             primitive_calls=[receipt])
    out=compact_feedback(raw)
    assert out['stdout']=='need changed viewpoint\n' and out['exception']==raw['exception']
    assert out['primitive_calls'][0]['verification_request_id']=='a'


def test_ranked_candidates_keep_new_calculations_and_reference_exact_ledger_diagnostics():
    import ast
    diagnostic=dict(code='collision',metrics=dict(obstacle_name='wall_x_low',
        segment_end_tcp_pose=[1.23456789]*16))
    printed=dict(outcome='no_candidate_executed',diagnostics=[dict(candidate=4,
        wrist_view={},score=.45,planning_diagnostic=diagnostic)])
    raw=dict(stdout=repr(printed)+'\n',primitive_calls=[dict(tool='preview_skill',planning_diagnostic=diagnostic)])
    before=deepcopy(raw);out=compact_feedback(raw);value=ast.literal_eval(out['stdout'])
    candidate=value['diagnostics'][0]
    assert candidate['candidate']==4 and candidate['score']==.45 and candidate['wrist_view']=={}
    reference=candidate['planning_diagnostic']
    assert out['primitive_calls'][reference['see_primitive_call_index']][reference['field']]==diagnostic
    assert raw==before and len(out['stdout'])<len(raw['stdout'])


def test_changed_diagnostic_not_replaced_by_same_code_reference():
    diagnostic=dict(code='collision',metrics=dict(segment_end_tcp_pose=[1.23456789]*16))
    changed=deepcopy(diagnostic);changed['metrics']['segment_end_tcp_pose'][0]+=.001
    raw=dict(stdout=repr(dict(diagnostics=[dict(planning_diagnostic=changed)])),
             primitive_calls=[dict(tool='preview_skill',planning_diagnostic=diagnostic)])
    assert compact_feedback(raw)==raw


def test_actual_model_print_aliases_reference_exact_values_only():
    import ast
    diagnostic=dict(code='collision',metrics=dict(segment_end_tcp_pose=[1.23456789]*16))
    placement=dict(status='predicted_blocked',obstruction=diagnostic)
    printed=dict(stage='preview',planned=False,diagnostics=diagnostic,placement=placement,
                 score=.2,visible=False)
    raw=dict(stdout=repr(printed)+'\n',primitive_calls=[dict(tool='preview_skill',
        planning_diagnostic=diagnostic,placement_advisory=placement)])
    before=deepcopy(raw);out=compact_feedback(raw);value=ast.literal_eval(out['stdout'])
    for alias,canonical in [('diagnostics','planning_diagnostic'),('placement','placement_advisory')]:
        ref=value[alias]
        assert ref['field']==canonical
        assert out['primitive_calls'][ref['see_primitive_call_index']][ref['field']]==printed[alias]
    assert value['score']==.2 and not value['visible'] and value['stage']=='preview'
    assert raw==before and out['primitive_calls']==raw['primitive_calls']
    changed=deepcopy(printed);changed['diagnostics']['metrics']['segment_end_tcp_pose'][0]+=.001
    raw['stdout']=repr(changed)+'\n'
    value=ast.literal_eval(compact_feedback(raw)['stdout'])
    assert value['diagnostics']==changed['diagnostics']
