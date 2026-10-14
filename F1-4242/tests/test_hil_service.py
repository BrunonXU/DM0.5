"""HIL status never invents a live link from a historical console."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hil_service import HILService, parse_tail, MAX_TAIL


def fixture_service(tmp_path, now=1000., active=True):
    runtime=tmp_path/'dagger';runtime.mkdir()
    (tmp_path/'hil.json').write_text(json.dumps(dict(schema='f1_hil_panel_v1',runtime=str(runtime),launcher='/home/user/Bruno/DAgger/start_f1_teleop.sh',ssh_host='user@192.168.31.58')))
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
