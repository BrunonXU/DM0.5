"""Offline assembly checks for one server with distinct base/actor contracts."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('runtime_rlt', ROOT / 'scripts/inference_runtime.py')
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)


@pytest.fixture
def catalog():
    c = json.loads((ROOT / 'artifacts/20261026-b1250-only/assembly/catalog.json').read_text()) if (
        ROOT / 'artifacts/20261026-b1250-only/assembly/catalog.json').exists() else json.loads(
        (ROOT / 'configs/inference_runtime_f1_09.json').read_text())
    key = c['default_hil']
    m = c['models'][key]
    c['modes']['rlt-hil'] = dict(c['modes']['hil'], rtc_flags=['--rlt-hil', '--episode-controls'])
    m['modes']['rlt-hil'] = dict(alias='test-actor', runtime='/tmp/actor-runtime')
    c['default_rlt_hil'] = key
    c['contracts']['test_actor'] = {'health': {
        'protocol': 'f1_eef_rlt_sync_v1', 'ready': True, 'rtc': False, 'sync': True,
        'prediction_horizon': 16, 'reference_horizon': 50, 'execute_steps': 16}}
    binding = dict(base_model_version=m['model_version'], actor_manifest_sha256='d' * 64,
                   encoder_manifest_sha256='c' * 64, protocol='f1_eef_rlt_sync_v1')
    version = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    m['rlt'] = dict(head='/srv/actor-export', encoder='/srv/token-export', contract='test_actor',
        core_root='/srv/frozen-mlp-core',
        model_id='f1-toys-rlt-sync-' + version[:16], model_version=version, label='test actor', identity={
            'base_model_version': m['model_version'], 'actor_version': 'b' * 64,
            'actor_manifest_sha256': 'd' * 64, 'encoder_manifest_sha256': 'c' * 64,
            'execution_mode': 'sync_c16_hold_then_execute'},
        runtime_manifest={'schema': 'test_runtime', 'binding': binding, 'source_export_deployment_ready': False})
    return c


def test_mode_alias_cannot_silently_run_base(catalog):
    key, _ = r.resolve(catalog, 'test-actor')
    assert r.execution_mode(catalog, key, 'test-actor') == 'rlt-hil'
    with pytest.raises(ValueError, match='conflicts'):
        r.execution_mode(catalog, key, 'test-actor', 'rollout')
    assert r.execution_mode(catalog, key, catalog['models'][key]['alias']) == 'rollout'
    assert r.execution_mode(catalog, key, None, 'rlt-hil') == 'rlt-hil'
    assert r.execution_mode(catalog, key, None, 'hil') == 'hil'


def test_registered_rlt_preserves_current_base_modes():
    current = json.loads((ROOT / 'configs/inference_runtime_f1_09.json').read_text())
    # Compare adding RLT to today's pinned base, not the 26 Oct runtime before
    # the independently approved fault-lifecycle/manual-worker upgrades.
    previous = copy.deepcopy(current)
    for model in previous['models'].values():
        model.pop('rlt',None)
        model['modes'].pop('rlt-hil',None)
    r.validate(current)
    key, model = r.resolve(current, 'rlt400')
    assert key == current['default_hil'] == current['default_rlt_hil']
    assert len(current['models']) == len(previous['models']) == 1
    for mode in ('hil', 'rollout'):
        assert r.client_config(current, key, mode) == r.client_config(previous, key, mode)
        assert model['modes'][mode] == previous['models'][key]['modes'][mode]
    assert r.tunnel_command(current, key) == r.tunnel_command(previous, key)
    assert model['rlt']['identity']['checkpoint_step'] == 400
    # V41 deliberately activates the validated serving graph and RTC contract.
    assert model['rlt']['runtime_manifest']['reference_compute_path'] == 'serving_graph_prefix_and_suffix'
    assert model['rlt']['identity']['execution_mode'] == 'rtc_c16_committed_prefix_v1'
    assert model['rlt']['rtc'] is True
    assert model['rlt']['rtc_delay_steps'] == 6
    assert model['rlt']['rtc_min_steps'] == 8
    assert '--rlt-rtc' in r.server_script(current, key)


def test_base_and_actor_configs_keep_separate_horizons_and_identities(catalog):
    key = catalog['default_hil']
    original = r.client_config(catalog, key, 'hil')
    cfg = r.client_config(catalog, key, 'rlt-hil')
    assert cfg['policy'] == original['policy']
    assert cfg['rlt_policy']['chunk_steps'] == 16
    assert cfg['rlt_policy']['endpoint'] == cfg['policy']['endpoint'] + '/rlt'
    assert cfg['rlt_policy']['model_id'] != cfg['policy']['model_id']
    assert cfg['rlt_policy']['expected_rlt'] == catalog['models'][key]['rlt']['identity']
    cfg['rlt_policy']['expected_rlt']['actor_version'] = 'changed'
    assert catalog['models'][key]['rlt']['identity']['actor_version'] != 'changed'


def test_rlt_rtc_assembly_uses_distinct_identity_and_preserves_base(catalog):
    key=catalog['default_hil'];extension=catalog['models'][key]['rlt']
    before=r.client_config(catalog,key,'hil')
    extension['rtc']=True
    binding=extension['runtime_manifest']['binding'];binding['protocol']='f1_eef_rlt_rtc_v1'
    version=hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    extension.update(model_id='f1-toys-rlt-rtc-'+version[:16],model_version=version)
    extension['identity']['execution_mode']='rtc_c16_committed_prefix_v1'
    catalog['contracts']['test_actor']['health'].update(protocol='f1_eef_rlt_rtc_v1',rtc=True,sync=False)
    r.validate(catalog)
    cfg=r.client_config(catalog,key,'rlt-hil')
    assert cfg['rlt_policy']['rlt_rtc'] and cfg['rlt_policy']['rlt_rtc_min_steps']==8
    assert cfg['policy']==before['policy']
    assert '--rlt-rtc' in r.server_script(catalog,key)


def test_compute_optimization_is_explicit_in_script_and_manifest(catalog):
    key=catalog['default_hil'];rlt=catalog['models'][key]['rlt']
    rlt['reference_compute_path']='serving_graph_prefix_and_suffix'
    rlt['runtime_manifest']['reference_compute_path']='training_eager_prefix_and_suffix'
    with pytest.raises(ValueError,match='compute configuration differ'):r.validate(catalog)
    rlt['runtime_manifest']['reference_compute_path']=rlt['reference_compute_path']
    r.validate(catalog)
    assert '--rlt-reference-compute serving_graph_prefix_and_suffix' in r.server_script(catalog,key)


def test_single_server_single_tunnel_actor_extension(catalog, tmp_path):
    key = catalog['default_hil']
    command = r.server_script(catalog, key)
    assert command.count('exec python ') == 1
    assert command.count('--port ') == 1
    assert '--rlt-head /srv/actor-export --rlt-encoder /srv/token-export' in command
    assert '--rlt-rlinf-root /srv/frozen-mlp-core' in command
    before = r.tunnel_command(catalog, key)
    old = copy.deepcopy(catalog)
    del old['models'][key]['rlt']
    assert r.tunnel_command(old, key) == before
    out = tmp_path / 'assembly'
    r.build(catalog, out)
    assert len(list((out / 'servers').glob('*.sh'))) == len(catalog['models'])
    r.verify_assembly(out)
    manifest = json.loads((out / 'runtime-manifests' / f'{key}-rlt.json').read_text())
    assert manifest['source_export_deployment_ready'] is False
    result = subprocess.run([sys.executable, str(out / 'inference_runtime.py'),
                             '--model', 'test-actor', '--describe'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    argv = json.loads(result.stdout)['client_command']
    assert '--rlt-hil' in argv and '--episode-controls' in argv
    assert '--rtc' not in argv and '--rtc-hil' not in argv
    assert argv[argv.index('--rtc-delay-steps') + 1] == '14'


def test_boundary_update_requires_registry_and_is_explicit(catalog):
    key = catalog['default_hil']
    extension = catalog['models'][key]['rlt']
    assert r.client_config(catalog, key, 'rlt-hil')['rlt_policy']['allow_boundary_updates'] is False
    extension['allow_boundary_updates'] = True
    with pytest.raises(ValueError, match='registry'):
        r.validate(catalog)
    extension['snapshot_root'] = '/srv/local-actors'
    r.validate(catalog)
    assert r.client_config(catalog, key, 'rlt-hil')['rlt_policy']['allow_boundary_updates'] is True
    assert '--rlt-snapshots /srv/local-actors' in r.server_script(catalog, key)


@pytest.mark.parametrize('which,field,value', [
    ('base', 'model_version', 'changed'), ('actor', 'model_id', 'changed'),
    ('actor', 'rtc', True), ('actor', 'prediction_horizon', 50),
    ('actor', 'rlt', {'actor_version': 'changed'}),
])
def test_both_live_identities_are_checked(catalog, monkeypatch, which, field, value):
    key = catalog['default_hil']
    m = catalog['models'][key]
    health = {
        'base': dict(catalog['contracts'][m['contract']]['health'],
                     model_id=m['model_id'], model_version=m['model_version']),
        'actor': dict(catalog['contracts'][m['rlt']['contract']]['health'],
                     model_id=m['rlt']['model_id'], model_version=m['rlt']['model_version'],
                     rlt=copy.deepcopy(m['rlt']['identity']))}
    paths = []

    def response(url, **kwargs):
        paths.append(url)
        return io.StringIO(json.dumps(health['actor' if url.endswith('/rlt/health') else 'base']))

    monkeypatch.setattr(r, 'urlopen', response)
    assert r.check_health(catalog, key, 'rlt-hil') == health
    assert len(paths) == 2 and paths[-1].endswith('/rlt/health')
    health[which][field] = value
    with pytest.raises(ValueError, match='contract mismatch'):
        r.check_health(catalog, key, 'rlt-hil')


def test_invalid_mixed_execution_contract_rejected(catalog):
    c = copy.deepcopy(catalog)
    c['modes']['rlt-hil']['rtc_flags'].append('--rtc-hil')
    with pytest.raises(ValueError, match='separate'):
        r.validate(c)
    c = copy.deepcopy(catalog)
    c['models'][c['default_hil']]['rlt']['identity']['base_model_version'] = 'wrong-parent'
    with pytest.raises(ValueError, match='base model'):
        r.validate(c)
    c = copy.deepcopy(catalog)
    c['models'][c['default_hil']]['rlt']['model_version'] = '0' * 64
    with pytest.raises(ValueError, match='runtime manifest'):
        r.validate(c)


@pytest.mark.parametrize('actor_matches', [True, False])
def test_machine_activation_checks_actor_before_changing_entry(catalog, tmp_path, actor_matches):
    import contextlib
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location('deploy_rlt', ROOT / 'scripts/deploy_inference_runtime.py')
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    key = catalog['default_hil']
    m = catalog['models'][key]
    run = tmp_path / 'run'
    run.mkdir()
    m['run'] = str(run)
    entry = run / 'start.sh'
    entry.write_text('original launcher')
    catalog['machines'][m['machine']]['files_sha256'] = {}
    assembly = tmp_path / 'assembly'
    r.build(catalog, assembly)
    snapshot = tmp_path / 'before.json'
    snapshot.write_text(json.dumps({'files': {str(entry): entry.read_text()}}))
    base = dict(catalog['contracts'][m['contract']]['health'], model_id=m['model_id'], model_version=m['model_version'])
    actor = dict(catalog['contracts'][m['rlt']['contract']]['health'], model_id=m['rlt']['model_id'],
                 model_version=m['rlt']['model_version'], rlt=m['rlt']['identity'])
    if not actor_matches:
        actor['model_version'] = 'other actor checkpoint'

    class Remote:
        def python(self, code):
            shim = ('import io\ndef urlopen(url, **kwargs):\n return io.StringIO(' +
                    repr(json.dumps(actor)) + ' if url.endswith("/rlt/health") else ' + repr(json.dumps(base)) + ')')
            code = code.replace('from urllib.request import urlopen', shim)
            result = io.StringIO()
            with contextlib.redirect_stdout(result):
                exec(compile(code, '<activation>', 'exec'), {})
            return result.getvalue()

    args = SimpleNamespace(before=snapshot, receipts=tmp_path, machine=m['machine'])
    if actor_matches:
        deploy.activate_machine(args, catalog, Remote(), str(assembly))
        assert entry.read_text() == (assembly / 'servers' / f'{key}.sh').read_text()
        receipt = json.loads((tmp_path / f'{m["machine"]}-activation.json').read_text())
        assert receipt['checks'][0]['actor_model_id'] == actor['model_id']
    else:
        with pytest.raises(ValueError, match='RLT server identity'):
            deploy.activate_machine(args, catalog, Remote(), str(assembly))
        assert entry.read_text() == 'original launcher'
        assert not assembly.with_name('assembly-activation-backup').exists()
