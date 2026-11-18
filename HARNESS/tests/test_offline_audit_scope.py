"""Hardware contract flags alone must never classify real outputs as a replay."""
import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('offline_audit_scope',
    Path(__file__).parents[1]/'scripts/offline_audit_scope.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def fixture():
    report=dict(physical_motion=False,robot_connected=False,mode='hardware',
        scenario=dict(follower='approved-output'),
        offline_hardware_contract_fixture=dict(actuator_transport='MemoryTransport'),
        final_status=dict(recording_identity=dict(offline_fixture=True,actuator_transport='memory')))
    events=[dict(kind='offline_hardware_contract_fixture',simulation=True,
                 physical_motion=False,actuator_transport='memory')]
    return report,events,[dict(simulation=True)]


def test_explicit_local_hardware_contract_is_auditable_without_rewriting_mode():
    args=fixture();module.require_offline_output_report(*args)
    assert args[0]['mode']=='hardware'


@pytest.mark.parametrize('fault',['motion','connection','transport','identity','marker','event','output','empty'])
def test_missing_or_physical_provenance_is_rejected(fault):
    report,events,outputs=fixture()
    if fault=='motion':report['physical_motion']=True
    elif fault=='connection':report['robot_connected']=True
    elif fault=='transport':report['offline_hardware_contract_fixture']['actuator_transport']='ROS'
    elif fault=='identity':report['final_status']['recording_identity']={}
    elif fault=='marker':events=[]
    elif fault=='event':events.append(dict(simulation=False))
    elif fault=='output':outputs[0]['simulation']=False
    else:outputs=[]
    with pytest.raises(ValueError):module.require_offline_output_report(report,events,outputs)
