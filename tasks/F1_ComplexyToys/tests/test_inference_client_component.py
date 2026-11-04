"""Per-model deployment assets cannot silently change control-mode semantics."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

TASK = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_components', TASK/'scripts/inference_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
CATALOG = json.loads((TASK/'artifacts/20261023-rlinf-deploy/catalog-before.json').read_text())


def test_selected_component_changes_only_one_model_and_preserves_rtc_flags(tmp_path):
    c=copy.deepcopy(CATALOG)
    key='v1_dagger_stage_b_1250'
    original=runtime.client_component(c,key,'rollout')
    c['models'][key]['modes']['rollout']['client_component']={'client':str(tmp_path),'manifest_sha256':'a'*64}
    runtime.validate(c)
    selected=runtime.client_component(c,key,'rollout')
    assert {k:v for k,v in selected.items() if k not in ('client','manifest_sha256')} == {k:v for k,v in original.items() if k not in ('client','manifest_sha256')}
    assert runtime.client_config(c,key,'rollout')['accepted_release_manifest_sha256']=='a'*64
    command=runtime.client_command(c,key,'rollout',tmp_path,shadow=True,duration=5)
    assert command[1]==str(tmp_path/'scripts/50_f1_hil.sh')
    assert '--execute' not in command and '--rtc' in command
    assert runtime.client_config(c,'v1_full_1000','rollout')==runtime.client_config(CATALOG,'v1_full_1000','rollout')
    assert runtime.client_component(c,'v1_full_1000','rollout')==CATALOG['modes']['rollout']


@pytest.mark.parametrize('change',[{'rtc_flags':[]},{'manifest_sha256':'bad'},{'client':'relative/path'},{'manifest_sha256':None}])
def test_component_rejects_semantics_override_or_unpinned_assets(change):
    c=copy.deepcopy(CATALOG)
    c['models']['v1_dagger_stage_b_1250']['modes']['rollout']['client_component']={'client':'/release/accepted','manifest_sha256':'b'*64,**change}
    with pytest.raises(ValueError,match='component override'):
        runtime.validate(c)


def test_verification_checks_selected_manifest_and_files(tmp_path):
    c=copy.deepcopy(CATALOG);key='v1_dagger_stage_b_1250'
    path=tmp_path/'client.py';path.write_text('verified code')
    manifest=tmp_path/'F1_RELEASE_MANIFEST.json'
    manifest.write_text(json.dumps({'files':{'client.py':hashlib.sha256(path.read_bytes()).hexdigest()}}))
    c['models'][key]['modes']['rollout']['client_component']={'client':str(tmp_path),'manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest()}
    runtime.verify_component(c,'rollout',key)
    path.write_text('changed code')
    with pytest.raises(ValueError,match='Pinned client changed'):
        runtime.verify_component(c,'rollout',key)
