import json

import pytest

from test_development_admission import full_site, site  # noqa: F401
from test_web_workspace import request
from f1_harness.contracts import Task, digest
from f1_harness.evidence_pack import file_hash
from f1_harness.development import REVIEWED_FILES
from f1_harness.programs import single_block_program
from f1_harness.web_snapshot import freeze_site


@pytest.mark.parametrize('budget', [0, 1, 2])
def test_run_configuration_is_bound_without_mutating_template(full_site, tmp_path, budget):  # noqa: F811
    root=full_site['root']
    (root/'coarse-calibration.json').write_bytes((root/'coarse.json').read_bytes())
    (root/'controller-reuse.json').write_bytes((root/'ik-reuse.json').read_bytes())
    (root/'parameter-sources.json').write_text('{}')
    before={name:file_hash(root/name) for name in REVIEWED_FILES}
    run_name='web-'+'a'*32
    remote='/home/user/Bruno/HARNESS/site/'+run_name
    output=tmp_path/'new-site'
    task=freeze_site(root,output,remote_site=remote,request=request(budget),run_name=run_name)
    admission=json.loads((output/'development.local.json').read_text())
    assert task.goal=='placed' and task.local_corrections==budget
    assert task.operator_instruction==request(budget)['config']['instruction'].strip()
    assert admission['scope']['local_corrections']==budget
    assert admission['task_hash']==digest(task)
    assert admission['reviewed_files']=={name:file_hash(output/name) for name in REVIEWED_FILES}
    assert before=={name:file_hash(root/name) for name in REVIEWED_FILES}
    for name in ('plan','verify'):
        value=json.loads((output/(name+'.local.json')).read_text())
        assert Task.model_validate(value['task'])==task
    plan=json.loads((output/'plan.local.json').read_text())
    assert plan['fixture']['frame_chain']['development_owner_config']==remote+'/owner.local.json'
    assert 'range('+str(budget)+')' in single_block_program(task)
    assert 'verify_placed' in single_block_program(task)
    owner=json.loads((output/'owner.local.json').read_text())
    gateway=json.loads((output/'gateway.local.json').read_text())
    verify=json.loads((output/'verify.local.json').read_text())
    numerics=json.loads((output/'numerics.local.json').read_text())
    ipc='/home/user/Bruno/HARNESS/runtime/'+run_name
    assert owner['owner_socket']==gateway['owner_socket']==gateway['scene']['owner_socket']==ipc+'/owner.sock'
    assert owner['planner_socket']==plan['socket']==ipc+'/plan.sock'
    assert owner['verifier_socket']==verify['socket']==gateway['scene']['verifier_socket']==ipc+'/verify.sock'
    assert plan['numerical_socket']==numerics['socket']==gateway['scene']['numerical_socket']==ipc+'/numerics.sock'
    assert len((ipc+'/numerics.sock').encode())<108


def test_changed_template_is_rejected_before_copy(full_site,tmp_path):  # noqa: F811
    root=full_site['root']
    path=root/'task.local.json'
    path.write_text(path.read_text()+' ')
    # All source files must first be regular files; provide expected auxiliary names.
    for source,target in [('coarse.json','coarse-calibration.json'),('ik-reuse.json','controller-reuse.json')]:
        (root/target).write_bytes((root/source).read_bytes())
    (root/'parameter-sources.json').write_text('{}')
    with pytest.raises(ValueError,match='reviewed_template_changed'):
        freeze_site(root,tmp_path/'out',remote_site='/home/user/Bruno/HARNESS/site/web-'+'a'*32,
                    request=request(),run_name='web-'+'a'*32)


def test_runs_have_distinct_IPC_and_never_remove_existing_endpoint(full_site,tmp_path):  # noqa: F811
    root=full_site['root']
    for source,target in [('coarse.json','coarse-calibration.json'),('ik-reuse.json','controller-reuse.json')]:
        (root/target).write_bytes((root/source).read_bytes())
    (root/'parameter-sources.json').write_text('{}')
    stale=tmp_path/'owner.sock';stale.write_bytes(b'preserved prior owner endpoint')
    for name in ('owner.local.json','gateway.local.json'):
        text=(root/name).read_text().replace('"/owner"',json.dumps(str(stale)))
        (root/name).write_text(text)
    record=json.loads((root/'development.local.json').read_text())
    record['reviewed_files']={name:file_hash(root/name) for name in REVIEWED_FILES}
    (root/'development.local.json').write_text(json.dumps(record))
    before={name:file_hash(root/name) for name in REVIEWED_FILES}
    sockets=[]
    for suffix in ('a','b'):
        run='web-'+suffix*32;output=tmp_path/run
        freeze_site(root,output,remote_site='/home/user/Bruno/HARNESS/site/'+run,
                    request=request(),run_name=run)
        sockets.append(json.loads((output/'owner.local.json').read_text())['owner_socket'])
        launch=json.loads((output/'launch.local.json').read_text())
        original=json.loads((root/'launch.local.json').read_text())
        assert launch.get('control_lock')==original.get('control_lock')
        assert launch.get('runtime')==original.get('runtime')
    assert sockets[0]!=sockets[1]
    assert stale.read_bytes()==b'preserved prior owner endpoint'
    assert before=={name:file_hash(root/name) for name in REVIEWED_FILES}


@pytest.mark.parametrize('component,code,expected',[
    ('owner',73,'owner IPC路径已存在'),('owner',1,'组件未写出启动日志'),
    ('robot_services',73,'组件未写出启动日志')])
def test_empty_component_log_preserves_exit_code(tmp_path,component,code,expected):
    from types import SimpleNamespace
    from f1_harness.web_process import WorkspaceProcesses
    processes=WorkspaceProcesses.__new__(WorkspaceProcesses)
    processes.run=tmp_path
    processes.children=[({'name':component},SimpleNamespace(poll=lambda:code),None)]
    (tmp_path/(component+'.log')).write_bytes(b'')
    with pytest.raises(RuntimeError) as failure:processes.check()
    assert 'exit='+str(code) in str(failure.value)
    assert expected in str(failure.value)
