"""HIL status never invents a live link from a historical console."""
import json
import os
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hil_service import HILService, parse_tail, MAX_TAIL, guidance


def fixture_service(tmp_path, now=1000., active=True):
    runtime=tmp_path/'dagger';runtime.mkdir()
    (tmp_path/'hil.json').write_text(json.dumps(dict(schema='f1_hil_panel_v1',runtime=str(runtime),launcher='/home/user/Bruno/DAgger/start_f1_teleop.sh',ssh_host='user@192.0.2.3')))
    clock=[now]
    procs=[dict(pid=123,start_ticks='10',started_unix=990.)] if active else []
    service=HILService(tmp_path, clock=lambda:clock[0], process_reader=lambda:procs)
    log=runtime/'onsite-powered-console.log'
    def write(text,mtime=999.):
        log.write_text(text);os.utime(log,(mtime,mtime))
    return service,clock,procs,write


def test_history_and_reused_process_never_show_live(tmp_path):
    service,clock,procs,write=fixture_service(tmp_path,active=False)
    write('F1 STATUS '+json.dumps(dict(state='hil_latched',input_fresh=True,execution='POWERED')))
    state=service.state();assert state['phase']=='idle' and not state['summary_fresh'] and not state['status']
    procs.append(dict(pid=123,start_ticks='99',started_unix=1001.));clock[0]=1002
    assert service.state()['phase']=='unknown'


def test_summary_expiry_clears_input_and_caches_bounded_state(tmp_path):
    service,clock,procs,write=fixture_service(tmp_path)
    write('F1 STATUS '+json.dumps(dict(state='hil_latched',input_fresh=True,side='right',rb=True)))
    state=service.state();assert state['summary_fresh'] and state['status']['rb']
    state['status']['rb']=False
    assert service.state()['status']['rb'] is True
    clock[0]=1010
    state=service.state();assert state['phase']=='unknown' and state['status']=={}


def test_new_identity_and_failure_override_old_green(tmp_path):
    service,clock,procs,write=fixture_service(tmp_path)
    old='F1 STATUS '+json.dumps(dict(state='hil_latched',input_fresh=True))+'\n'
    write(old+'F1 IDENTITY {"release":"new"}\nF1 upper16: local operator ready. Press Enter, then use F710 START to begin. ')
    state=service.state();assert state['phase']=='awaiting_enter' and not state['summary_fresh']
    clock[0]=1002;write(old+'F1 FAILED: receiver is stopping\nRuntimeError: acquisition stopped\n',1001)
    state=service.state();assert state['phase']=='failed' and not state['status']


def test_bounded_tail_and_command_quoting(tmp_path):
    service,clock,procs,write=fixture_service(tmp_path)
    write('x'*(MAX_TAIL*2)+'\nF1 STATUS {"state":"waiting_for_teleop_start","input_fresh":true}\n')
    state=service.state();assert state['phase']=='waiting_for_teleop_start' and len(state['commands'])==3
    assert 'START' in state['next_step']
    assert all('password' not in x['command'] for x in state['commands'])


def test_conflict_offline_and_missing_configuration(tmp_path):
    empty=HILService(tmp_path,offline=True);assert empty.state()['phase']=='not_configured'
    service,clock,procs,write=fixture_service(tmp_path);procs.append(dict(pid=124,start_ticks='11',started_unix=990.))
    assert service.state()['phase']=='conflict'
    offline=HILService(tmp_path,offline=True,process_reader=lambda:(_ for _ in ()).throw(AssertionError('process scan')))
    assert offline.state()['phase']=='idle'


def test_parser_drops_previous_session_failure():
    value=parse_tail('RuntimeError: old\nF1 IDENTITY {"release":"new"}\n')
    assert value['failure'] is None and value['identity']['release']=='new'


def test_unrelated_log_writes_do_not_refresh_old_status(tmp_path):
    service,clock,procs,write=fixture_service(tmp_path)
    summary='F1 STATUS {"state":"hil_latched","input_fresh":true}\n'
    write(summary);assert service.state()['summary_fresh']
    clock[0]=1010;write(summary+'unrelated diagnostic\n',1009)
    state=service.state();assert state['phase']=='unknown' and not state['status']


def test_routes_read_only_purpose_and_model_gate(tmp_path):
    from hil_contract import validate_session
    service,*_=fixture_service(tmp_path,active=False)
    launcher=tmp_path/'launch.sh';launcher.write_text('#!/bin/sh\nexit 99\n')
    items={}
    for key,purpose in [('play','teleop_play'),('demonstration','demonstration'),('policy','policy_hil')]:
        p=tmp_path/(key+'.json');p.write_text(json.dumps(validate_session(dict(purpose=purpose,auto_video=True))))
        items[key]=dict(launcher=str(launcher),session_config=str(p))
    (tmp_path/'hil-routes.json').write_text(json.dumps(dict(routes=items)))
    before={str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    rows=service.routes();assert [r['ready'] for r in rows]==[True,True,False]
    assert rows[2]['reason']=='模型未绑定'
    assert all(len(r['commands'])==3 for r in rows)
    assert rows[0]['commands'][0]['command'].endswith(' play')
    assert not any('endpoint' in r or 'task' in r for r in rows)
    assert before=={str(p):p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    # A route may not silently become a differently purposed session.
    (tmp_path/'play.json').write_text(json.dumps(validate_session({'purpose':'policy_hil'})))
    assert not service.routes()[0]['ready']
    config=validate_session({'purpose':'policy_hil','policy':{'endpoint':'http://model:8091'}})
    (tmp_path/'policy.json').write_text(json.dumps(config))
    assert service.routes()[2]['ready']


def test_play_is_not_video_or_training(tmp_path):
    from hil_contract import validate_session
    assert validate_session({'purpose':'teleop_play','auto_video':True})['auto_video'] is False


def test_isolated_rtc_runtime_and_finish_are_bound_to_active_process(tmp_path):
    from hil_contract import validate_session
    service, clock, procs, _ = fixture_service(tmp_path)
    candidate = tmp_path/'rtc-hil'; candidate.mkdir()
    launcher = tmp_path/'rtc.sh'; launcher.write_text('exit 99')
    session = tmp_path/'policy.json'
    session.write_text(json.dumps(validate_session(dict(purpose='policy_hil', policy=dict(endpoint='http://model:8153')))))
    (tmp_path/'hil-routes.json').write_text(json.dumps(dict(routes=dict(policy=dict(
        launcher=str(launcher), session_config=str(session), runtime=str(candidate), rtc_hil=True)))))
    procs[0]['runtime'] = str(candidate)
    identity = dict(pid=123, start_ticks='10', run_id='rtc-test')
    (candidate/'status.json').write_text(json.dumps(dict(schema='f1_hil_status_v1',
        **identity, updated_unix=1000., binding=dict(rtc_hil=True),
        status=dict(state='hil_latched'), capabilities=['finish_session_v1'])))
    state = service.state()
    assert state['summary_fresh'] and state['binding']['rtc_hil']
    command = service.routes()[2]['commands'][0]['command']
    assert command.splitlines()[0] == 'bash '+str(launcher)+' --continuous'
    assert '--shadow' not in command.splitlines()[0]
    assert '需要零输出专项检查时添加 --shadow' in command
    service.finish(dict(session_key=identity))
    assert json.loads((candidate/'finish-request.json').read_text())['run_id'] == 'rtc-test'
    assert not (tmp_path/'dagger/finish-request.json').exists()
    procs[0]['runtime'] = str(tmp_path/'unregistered')
    clock[0] += 1
    assert not service.state()['summary_fresh']


@pytest.mark.parametrize('selected', ['stageb1250', 'dagger1000'])
def test_installed_policy_models_bind_status_and_finish_to_selected_runtime(tmp_path, selected):
    service, clock, procs, _ = fixture_service(tmp_path)
    models = {name: dict(model_id='model-'+name, runtime=str(tmp_path/name))
              for name in ('stageb1250', 'dagger1000')}
    # The UI's default policy is B; explicitly selecting the parent remains valid.
    (tmp_path/'hil-routes.json').write_text(json.dumps(dict(
        routes=dict(policy=dict(runtime=models['stageb1250']['runtime'])),
        installed_policy_models=models)))
    for name, model in models.items():
        root = Path(model['runtime']); root.mkdir()
        (root/'status.json').write_text(json.dumps(dict(schema='f1_hil_status_v1',
            pid=123, start_ticks='10', run_id='run-'+name, updated_unix=1000.,
            binding=dict(rtc_hil=True, model_id=model['model_id']),
            status=dict(state='hil_latched'), capabilities=['finish_session_v1'])))
    selected_root = Path(models[selected]['runtime'])
    procs[0]['runtime'] = str(selected_root)
    state = service.state()
    assert state['summary_fresh'] and state['receiver_runtime'] == str(selected_root)
    assert state['binding']['model_id'] == models[selected]['model_id']
    service.finish(dict(session_key=state['session_key']))
    assert json.loads((selected_root/'finish-request.json').read_text())['run_id'] == 'run-'+selected
    other = 'dagger1000' if selected == 'stageb1250' else 'stageb1250'
    assert not (Path(models[other]['runtime'])/'finish-request.json').exists()
    assert not (tmp_path/'dagger/finish-request.json').exists()

    # Even a valid, fresh snapshot cannot authorize an uninstalled runtime.
    unknown = tmp_path/'unregistered'; unknown.mkdir()
    (unknown/'status.json').write_bytes((selected_root/'status.json').read_bytes())
    procs[0]['runtime'] = str(unknown); clock[0] += 1
    state = service.state()
    assert not state['summary_fresh'] and 'not installed' in state['error']
    with pytest.raises(ValueError, match='会话已变化'):
        service.finish(dict(session_key=dict(pid=123, start_ticks='10', run_id='run-'+selected)))
    assert not (unknown/'finish-request.json').exists()


@pytest.mark.parametrize('models', [[], {'bad': {}},
    {'bad': {'model_id': 'model', 'runtime': 'relative/runtime'}},
    {'bad': {'model_id': '', 'runtime': '/tmp/model-runtime'}}])
def test_invalid_installed_policy_registry_cannot_supply_status(tmp_path, models):
    service, _, _, _ = fixture_service(tmp_path)
    (tmp_path/'hil-routes.json').write_text(json.dumps(dict(installed_policy_models=models)))
    state = service.state()
    assert state['phase'] == 'unknown' and not state['summary_fresh']
    assert 'Installed policy' in state['error']


def test_missing_episode_protocol_never_prompts_start_again(tmp_path):
    service,clock,procs,write=fixture_service(tmp_path)
    write('F1 STATUS '+json.dumps(dict(state='episode_ready',hold_reason='episode_sender_update_required',input_fresh=True,start_toggle=11,recording=dict(phase='idle'))))
    state=service.state()
    assert state['phase']=='episode_sender_update_required'
    assert 'update_episodes.ps1' in state['next_step']
    assert '反复按START无效' in state['next_step']
    # The stopped process must not be presented as a fresh blocking condition.
    procs.clear();clock[0]+=1
    state=service.state()
    assert not state['active'] and state['phase']=='idle'


def test_recoverable_pause_keeps_episode_and_gives_rb_instructions(tmp_path):
    service,clock,procs,write=fixture_service(tmp_path)
    for i,phase in enumerate(('input_wait_reconnect','input_wait_release_rb',
            'input_wait_fresh_packet','input_wait_press_rb','input_wait_feedback',
            'input_wait_safe_anchor','input_wait_update_sender')):
        clock[0]=1000.+i
        write('F1 STATUS '+json.dumps(dict(state=phase,input_fresh=True,
            purpose='demonstration',recovery_reason='manual_candidate_collision',
            recovery_required='release_neutral_rb',recording=dict(phase='recording'))),clock[0])
        state=service.state()
        assert state['phase']==phase
        assert '同一条仍在录制' in state['next_step']
        assert '不要按 START 恢复' in state['next_step']
        assert '碰撞风险' in state['next_step']
        if phase=='input_wait_reconnect':assert '等待 sender 和手柄重新连接' in state['next_step']
        if phase=='input_wait_safe_anchor':assert '当前实测姿态未通过检查' in state['next_step']
    text=guidance('input_wait_release_rb',dict(purpose='teleop_play',recording=dict(phase='recording')))
    assert '自由接入不录制' in text and '同一条仍在录制' not in text


def test_hard_stop_is_not_presented_as_start_to_resume():
    text=guidance('safe_hold',dict(recording=dict(phase='saving')))
    assert '正在封存' in text and 'START 只会开新条' in text
    text=guidance('safe_hold',dict(recording=dict(phase='idle')))
    assert '当前未录制' in text and '不会续录旧条' in text
    for reason in ('ik_rejected:unreachable','policy_failed:timeout','rtc_empty'):
        text=guidance('safe_hold',dict(hold_reason=reason,recording=dict(phase='recording')))
        assert '本条仍在录制' in text and 'START 转人工接管' in text
        assert 'START 只会开新条' not in text
