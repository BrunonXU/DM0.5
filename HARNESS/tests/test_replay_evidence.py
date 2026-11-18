"""Native failure flags must not become fabricated physical or synthetic success."""
import importlib.util
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location('replay_evidence',
    Path(__file__).parents[1]/'scripts/replay_evidence.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def test_failed_native_annotation_is_preserved_and_separate_from_execution_scope():
    outcomes=[dict(skill='grasp',result=dict(synthetic=False,outcome='failure'))]
    result=module.replay_provenance('fake',outcomes,[dict(simulation=True)],[],[])
    assert result['simulation_scope_confirmed']
    assert not result['all_results_synthetic'] and not result['real_experience_eligible']
    assert result['result_annotation_missing_or_false']==['grasp']
    assert outcomes[0]['result']==dict(synthetic=False,outcome='failure')


@pytest.mark.parametrize('field',['mode','events','publications','owner_outputs'])
def test_mixed_execution_provenance_is_not_confirmed(field):
    args=dict(mode='fake',outcomes=[dict(skill='grasp',result=dict(synthetic=True))],
              events=[dict(simulation=True)],publications=[],owner_outputs=[])
    args[field]='hardware' if field=='mode' else [dict(simulation=False)]
    result=module.replay_provenance(**args)
    assert not result['simulation_scope_confirmed'] and not result['real_experience_eligible']


def test_empty_trace_or_outcomes_cannot_imply_valid_synthetic_results():
    result=module.replay_provenance('fake',[],[],[],[])
    assert not result['all_results_synthetic'] and not result['simulation_scope_confirmed']


def test_complete_annotation_and_execution_scope_are_both_reported():
    result=module.replay_provenance('fake',[dict(skill='grasp',result=dict(synthetic=True))],
        [dict(simulation=True)],[dict(simulation=True)],[dict(simulation=True)])
    assert result['all_results_synthetic'] and result['simulation_scope_confirmed']
    assert not result['real_experience_eligible']
