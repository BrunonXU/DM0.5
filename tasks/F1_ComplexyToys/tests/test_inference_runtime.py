import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('inference_runtime',ROOT/'scripts/inference_runtime.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
# Historical equivalence tests bind the deployed old assembly, not today's model catalog.
C=json.loads((ROOT/'artifacts/20261022-unified-runtime/assembly-r3/catalog.json').read_text())
BEFORE=json.loads((ROOT/'artifacts/20261022-unified-runtime/f1-before.json').read_text())
H20=json.loads((ROOT/'artifacts/20261022-unified-runtime/h20-before.json').read_text())

@pytest.mark.parametrize('key',C['models'])
def test_full_config_equivalence(key):
    step=key.rsplit('_',1)[1]
    old=json.loads(BEFORE['files'][f'/home/user/Bruno/DAgger-runtime/f1-toys-20261016/full-h20/startup-read-v2/{step}/config.json'])
    assert r.client_config(C,key,'rollout')==old

def test_hil_config_equivalence():
    old=json.loads(BEFORE['files']['/home/user/Bruno/DAgger-runtime/rtc-hil-20261021-r6/config.json'])
    assert r.client_config(C,'v1_full_1000','hil')==old

@pytest.mark.parametrize('mode',['rollout','hil'])
@pytest.mark.parametrize('flags',[[],['--continuous'],['--duration-seconds','20'],['--shadow','--duration-seconds','5'],['--preflight']])
def test_actual_old_shell_argv_matches(mode,flags):
    old=BEFORE['files']['/home/user/Bruno/DAgger/start_f1_toys_'+('full_h20' if mode=='rollout' else 'rtc_hil')+'.sh']
    # Replace only external effects: source/ensure/mkdir and final console call.
    lines=[]
    for line in old.splitlines():
        if line.startswith('source '):
            if 'F1_CONSOLE_HELPER' in line:
                lines.append('f1_console_run() { shift; python3 -c \'import json,sys; print(json.dumps(sys.argv[1:]))\' "$@"; }')
            else:lines.append(':')
        elif line.startswith('python3 ') and 'ensure_' in line:lines.append(':')
        elif line.startswith('mkdir -p '):lines.append(':')
        else:lines.append(line)
    legacy=json.loads(subprocess.check_output(['bash','-c','\n'.join(lines),'test',*(['1000'] if mode=='rollout' else []),*flags],text=True))
    current=r.client_command(C,'v1_full_1000',mode,Path(C['robot_release']),shadow='--shadow' in flags,preflight='--preflight' in flags,duration=flags[flags.index('--duration-seconds')+1] if '--duration-seconds' in flags else None,continuous='--continuous' in flags)
    # Only the generated config pathname changes; exact contents tested above.
    current[current.index('--config')+1]=legacy[legacy.index('--config')+1]
    assert current==legacy

@pytest.mark.parametrize('key',C['models'])
def test_actual_server_argv_environment_equivalence(key):
    old=H20['files'][C['models'][key]['run']+'/start.sh']
    new=r.server_script(C,key)
    def run(text):
        # Capture argv/environment without importing model or creating directories.
        lines=[line for line in text.splitlines() if not line.startswith(('source ','mkdir '))]
        text='\n'.join(lines).replace('exec python ', 'python ')
        pre="python() { command python3 -c 'import json,os,sys; print(json.dumps(dict(argv=sys.argv[1:],env={k:v for k,v in os.environ.items() if k in [\"CUDA_VISIBLE_DEVICES\",\"PYTHONPATH\",\"TORCH_COMPILE_DISABLE\",\"OMP_NUM_THREADS\",\"MKL_NUM_THREADS\",\"OPENBLAS_NUM_THREADS\",\"NO_ALBUMENTATIONS_UPDATE\",\"TORCHINDUCTOR_CACHE_DIR\",\"TRITON_CACHE_DIR\"]})))' \"$@\"; };\n"
        return json.loads(subprocess.check_output(['bash','-c',pre+text],text=True))
    assert run(old)==run(new)

def test_new_checkpoint_needs_only_catalog_data():
    c=copy.deepcopy(C);m=copy.deepcopy(c['models']['v1_full_1000'])
    m.update(alias='new3000',model_id='new-identity',model_version='new-hash',local_port=8190,remote_port=8191,label='new_exp_3000')
    c['models']['new_exp_3000']=m
    assert r.resolve(c,'new3000')[0]=='new_exp_3000'
    assert r.client_config(c,'new_exp_3000','hil')['policy']['model_id']=='new-identity'
    assert '--port 8191' in r.server_script(c,'new_exp_3000')

@pytest.mark.parametrize('field,value',[('alias','full625'),('local_port',8152),('model_id','f1-toys-eef-9a66d2024a181fd4')])
def test_collision_rejected(field,value):
    c=copy.deepcopy(C);c['models']['v1_full_1000'][field]=value
    with pytest.raises(ValueError):r.validate(c)

def test_unregistered_hil_rejected():
    with pytest.raises(ValueError):r.client_config(C,'v1_full_625','hil')

def test_build_tamper_and_no_effect_modes(tmp_path):
    out=tmp_path/'assembly';r.build(C,out);r.verify_assembly(out)
    for flags in [['--list'],['--model','full1000','--describe']]:
        subprocess.run([sys.executable,str(out/'inference_runtime.py'),*flags],check=True,capture_output=True)
    (out/'catalog.json').write_text('{}')
    with pytest.raises(ValueError):r.verify_assembly(out)

def test_non_tty_fails_before_any_robot_read(tmp_path):
    out=tmp_path/'assembly';r.build(C,out)
    p=subprocess.run([sys.executable,str(out/'inference_runtime.py'),'--model','full625'],stdin=subprocess.DEVNULL,capture_output=True,text=True)
    assert p.returncode==2 and '现场交互' in p.stderr

@pytest.mark.parametrize('change',[{'model_id':'wrong'},{'model_version':'wrong'},{'rtc':False},{'ik_recovery':{'enabled':True}},{'input_preprocessing':{}}])
def test_health_mismatch_rejected(monkeypatch,change):
    import io
    h=dict(BEFORE['health']['full1000'],**change)
    monkeypatch.setattr(r,'urlopen',lambda *a,**k:io.StringIO(json.dumps(h)))
    with pytest.raises(ValueError):r.check_health(C,'v1_full_1000')

def test_machine_connection_is_independent():
    c=copy.deepcopy(C)
    c['machines']['other']=copy.deepcopy(c['machines']['h20'])
    c['machines']['other']['tunnel']['host']='another-verified-host'
    c['models']['v1_full_1000']['machine']='other'
    assert r.tunnel_command(c,'v1_full_1000')[-1]=='another-verified-host'
    assert r.tunnel_command(c,'v1_full_625')[-1]==C['machines']['h20']['tunnel']['host']

def test_server_dispatch_and_hash_guard(tmp_path):
    c=copy.deepcopy(C);profile=c['machines']['h20']
    server=tmp_path/'fake_server.py';server.write_text('import json,sys,os;print(json.dumps(dict(argv=sys.argv[1:],gpu=os.environ["CUDA_VISIBLE_DEVICES"])))')
    bindir=tmp_path/'bin';bindir.mkdir();(bindir/'python').symlink_to(sys.executable)
    activate=tmp_path/'activate.sh';activate.write_text('export PATH='+str(bindir)+':$PATH\n')
    profile.update(server=str(server),activate=str(activate),files_sha256={str(server):r.digest(server)})
    c['models']['v1_full_625']['run']=str(tmp_path/'run')
    out=tmp_path/'assembly';r.build(c,out)
    p=subprocess.run([sys.executable,str(out/'inference_runtime.py'),'--model','full625','--server'],capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    assert json.loads(p.stdout)['gpu']=='0'
    server.write_text('raise RuntimeError("must never run changed core")')
    p=subprocess.run([sys.executable,str(out/'inference_runtime.py'),'--model','full625','--server'],capture_output=True,text=True)
    assert p.returncode!=0 and 'Server component changed' in p.stderr and 'must never run' not in p.stderr

@pytest.mark.parametrize('initial',['old launcher',None,'concurrent change'])
def test_server_entry_activation_new_existing_and_concurrent_guard(tmp_path,initial):
    import contextlib
    import io
    from types import SimpleNamespace
    spec=importlib.util.spec_from_file_location('deploy_inference_runtime',ROOT/'scripts/deploy_inference_runtime.py')
    dep=importlib.util.module_from_spec(spec);spec.loader.exec_module(dep)
    root=tmp_path/'release';root.mkdir();(root/'servers').mkdir()
    run=tmp_path/'run';run.mkdir();entry=run/'start.sh'
    if initial is not None:entry.write_text(initial)
    c=dict(models={'test_model':dict(machine='h20',run=str(run),remote_port=8999,contract='test',model_id='test',model_version='v1')},machines={'h20':dict(files_sha256={})},contracts={'test':dict(health={})})
    (root/'catalog.json').write_text(json.dumps(c));(root/'assembly-manifest.json').write_text('{"files":{}}')
    (root/'servers/test_model.sh').write_text('generated launcher')
    before=tmp_path/'before.json';before.write_text(json.dumps(dict(files={str(entry):None if initial is None else 'old launcher'})))
    class Remote:
        def python(self,code):
            code=code.replace('from urllib.request import urlopen',"import io\ndef urlopen(*a,**k):return io.StringIO('{\"model_id\":\"test\",\"model_version\":\"v1\"}')")
            out=io.StringIO()
            with contextlib.redirect_stdout(out):exec(compile(code,'<remote installer>','exec'),{})
            return out.getvalue()
    args=SimpleNamespace(before=before,receipts=tmp_path,machine='h20')
    if initial=='concurrent change':
        with pytest.raises(ValueError,match='concurrently'):dep.activate_machine(args,c,Remote(),str(root))
        assert entry.read_text()==initial
        assert not root.with_name('release-activation-backup').exists()
    else:
        dep.activate_machine(args,c,Remote(),str(root))
        assert entry.read_text()=='generated launcher'
        assert (root.with_name('release-activation-backup')/('test_model.absent' if initial is None else 'test_model.sh')).exists()


def test_frozen_dagger_catalog_preserves_identity_and_runtime_contract():
    c=json.loads((ROOT/'artifacts/20261023-stage-ab-deployment/catalog-before.json').read_text())
    r.validate(c)
    assert set(c['models']) == {f'v1_dagger_rightonly_{step}' for step in (250,500,625,1000)}
    assert c['default_hil']=='v1_dagger_rightonly_1000'
    for key,m in c['models'].items():
        assert m['alias'].startswith('dagger')
        cfg=r.client_config(c,key,'rollout')
        assert cfg['hardware_capabilities']['left_gripper_present'] is False
        assert cfg['hardware_capabilities']['right_gripper_present'] is True
        assert cfg['policy']['rtc_pixels_directory']==m['pixels']
        assert '/f1-toys-v1-dagger-20261022-r3/bundle-' in m['bundle']
    for mode in c['modes'].values():
        assert (mode['delay_steps'],mode['min_steps'])==(14,16)
    for alias in ('full625','full1000','full1500','full2000'):
        with pytest.raises(ValueError):r.resolve(c,alias)


def test_historical_restored_full_uses_own_processor_with_explicit_missing_hardware_profile():
    c=json.loads((ROOT/'artifacts/20261026-hil-b1250/catalog-before.json').read_text())
    r.validate(c)
    key,m=r.resolve(c,'full1000')
    assert m['model_id']==C['models']['v1_full_1000']['model_id']
    assert m['bundle']==C['models']['v1_full_1000']['bundle']
    old=c['contracts'][m['contract']]
    assert old['health']['input_preprocessing']==C['contracts']['v1_three_camera_eef']['health']['input_preprocessing']
    profile=old['health']['state_input_profile']
    assert profile['physical_feedback_modified'] is False
    assert profile['state_valid_mask']==[True]*14+[False,True]
    assert '--state-input-profile legacy_left_gripper_zero_v1' in r.server_script(c,key)
    assert c['default_hil']=='v1_dagger_rightonly_1000'
    for name,model in c['models'].items():
        cfg=r.client_config(c,name,'rollout')
        assert cfg['hardware_capabilities']['left_gripper_present'] is False
        assert cfg['hardware_capabilities']['right_gripper_present'] is True
        if name!=key:
            assert model['contract']=='v1_dagger_rightonly_eef'
            assert '--state-input-profile' not in r.server_script(c,name)
    for mode in c['modes'].values():
        assert (mode['delay_steps'],mode['min_steps'])==(14,16)
    for alias in ('full625','full1500','full2000','dagger625'):
        with pytest.raises(ValueError):r.resolve(c,alias)


def test_supervised_terminal_preserves_real_authorization_and_reattaches(monkeypatch):
    c = copy.deepcopy(C)
    c['fault_lifecycle'] = 'f1_fault_lifecycle_v1'
    assert r.client_config(c, 'v1_full_1000', 'hil')['fault_lifecycle'] == c['fault_lifecycle']
    monkeypatch.delenv('F1_SUPERVISED_PROCESS', raising=False)
    monkeypatch.setattr(r.shutil, 'which', lambda _: '/usr/bin/tmux')
    monkeypatch.setattr(r.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a[0], 0))
    calls=[]
    import types
    monkeypatch.setitem(sys.modules, 'inference_terminal', types.SimpleNamespace(attach=lambda *a, **kw: calls.append((a, kw))))
    monkeypatch.setattr(r.sys, 'argv', ['entry', '--model', 'full1000', '--mode', 'hil', '--continuous'])
    r.supervised_terminal(c, 'v1_full_1000', 'hil', Path('/frozen'))
    args, options = calls[0]
    assert args[:2] == ('/usr/bin/tmux', 'f1-policy-v1_full_1000-hil')
    assert options['status_path'] is None  # Enter modes must attach immediately.
    assert 'F1_SUPERVISED_PROCESS=1' in args[-1]
    assert '--continuous' in args[-1] and 'START' not in args[-1]
    monkeypatch.setenv('F1_SUPERVISED_PROCESS', '1')
    r.supervised_terminal(c, 'v1_full_1000', 'hil', Path('/frozen'))
    assert len(calls) == 1
