import json
from pathlib import Path
import pytest
from f1dagger.identity import runtime_identity
from f1dagger.contract import sha256


def package(tmp_path):
    f=tmp_path/'src/f1dagger/example.py';f.parent.mkdir(parents=True,exist_ok=True);f.write_text('x=1\n')
    p=tmp_path/'F1_RELEASE_MANIFEST.json';p.write_text(json.dumps({'files':{'src/f1dagger/example.py':sha256(f)}}))
    return {'_config_sha256':'a'*64,'accepted_release_manifest_sha256':sha256(p)},f


def test_powered_requires_config_for_actual_release(tmp_path):
    config,f=package(tmp_path)
    assert runtime_identity(config,root=tmp_path,powered=True)['release_manifest_sha256']==config['accepted_release_manifest_sha256']
    config['accepted_release_manifest_sha256']='b'*64
    with pytest.raises(ValueError,match='different release'):runtime_identity(config,root=tmp_path,powered=True)


def test_tampered_or_extra_source_and_missing_acceptance_refused(tmp_path):
    config,f=package(tmp_path)
    f.write_text('x=2\n')
    with pytest.raises(ValueError,match='file mismatch'):runtime_identity(config,root=tmp_path)
    config,f=package(tmp_path)
    f.with_name('extra.py').write_text('x=3\n')
    with pytest.raises(ValueError,match='not covered'):runtime_identity(config,root=tmp_path)
    f.with_name('extra.py').unlink();config.pop('accepted_release_manifest_sha256')
    with pytest.raises(ValueError,match='manifest-bound'):runtime_identity(config,root=tmp_path,powered=True)
