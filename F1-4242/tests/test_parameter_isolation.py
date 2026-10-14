import json,subprocess
from types import SimpleNamespace
import pytest
from robot import runtime_contract as rc


def test_parameter_helper_keeps_parent_rmw_and_is_bounded(monkeypatch):
    monkeypatch.setenv('RMW_IMPLEMENTATION','rmw_cyclonedds_cpp');monkeypatch.setenv('CYCLONEDDS_URI','parent.xml')
    seen={};expected={'arm_control_mode':3}
    def run(args,**kw):
        seen.update(args=args,**kw)
        return SimpleNamespace(returncode=0,stdout=json.dumps({'ok':True,'result':{'values':expected,'rmw':'rmw_fastrtps_cpp'}}))
    monkeypatch.setattr(rc.subprocess,'run',run)
    result=rc.read_motor_parameters_isolated(expected,6,'rmw_fastrtps_cpp')
    assert seen['env']['RMW_IMPLEMENTATION']=='rmw_fastrtps_cpp' and 'CYCLONEDDS_URI' not in seen['env']
    assert rc.os.environ['RMW_IMPLEMENTATION']=='rmw_cyclonedds_cpp' and rc.os.environ['CYCLONEDDS_URI']=='parent.xml'
    assert seen['timeout']==9 and seen['args'][-1]=='robot.parameter_probe'
    assert json.loads(seen['input'])['expected']==expected and result['transport']=='isolated_read_only_process'


@pytest.mark.parametrize('stdout,code',[("not json",0),('{"ok":false,"error":"timeout"}',2),('{"ok":true,"result":{"values":{"arm_control_mode":1},"rmw":"rmw_fastrtps_cpp"}}',0)])
def test_invalid_helper_response_never_enables_contract(monkeypatch,stdout,code):
    monkeypatch.setattr(rc.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=stdout,returncode=code))
    with pytest.raises(RuntimeError):rc.read_motor_parameters_isolated({'arm_control_mode':3},6,'rmw_fastrtps_cpp')


def test_parameter_timeout_is_not_ignored(monkeypatch):
    def timeout(*a,**k):raise subprocess.TimeoutExpired(a[0],k['timeout'])
    monkeypatch.setattr(rc.subprocess,'run',timeout)
    with pytest.raises(subprocess.TimeoutExpired):rc.read_motor_parameters_isolated({},6,'rmw_fastrtps_cpp')
