import json
from pathlib import Path
import pytest
from recording.unified.storage import layout, read_config, migration_inventory


def config(tmp_path, **updates):
    data = dict(schema='f1_local_storage_v1', root=str(tmp_path/'data'), archive={'enabled': False})
    data.update(updates)
    p = tmp_path/'storage.json'
    p.write_text(json.dumps(data))
    return read_config(p)


def test_plan_is_local_and_does_not_create_dirs(tmp_path):
    c = config(tmp_path)
    assert c['paths']['policy_hil'] == str(tmp_path/'data/hil_data/policy_hil')
    assert not Path(c['root']).exists()
    assert migration_inventory(c)['uploads_started'] is False


def test_symlink_escape_and_relative_root_rejected(tmp_path):
    (tmp_path/'alias').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        layout(tmp_path/'alias/data')
    with pytest.raises(ValueError):
        layout('data')


def test_inventory_preserves_existing_data_and_reports_collisions(tmp_path):
    old = tmp_path/'legacy'; old.mkdir()
    (old/'episode').mkdir(); (old/'episode/raw').write_bytes(b'original')
    (old/'link').symlink_to(old/'episode', target_is_directory=True)
    new = tmp_path/'data/pico_data/episode'; new.mkdir(parents=True)
    c = config(tmp_path, legacy_roots={'pico': [str(old)]})
    result = migration_inventory(c)
    assert [r['state'] for r in result['entries']] == ['destination_exists', 'symlink_skipped']
    assert (old/'episode/raw').read_bytes() == b'original'
    assert not list(new.iterdir())
    assert result['executed'] is False


def test_config_cannot_silently_enable_transfer(tmp_path):
    with pytest.raises(ValueError, match='separately configured'):
        config(tmp_path, archive={'enabled': True})


def test_hil_new_root_keeps_legacy_and_runtime_roots(tmp_path):
    from types import SimpleNamespace
    from hil_data import roots
    p = tmp_path/'hil.json'
    p.write_text(json.dumps(dict(runtime=str(tmp_path/'runtime'),
        data_root=str(tmp_path/'data/hil_data'),
        legacy_data_roots=[str(tmp_path/'old'), str(tmp_path/'old')])) )
    found = roots(SimpleNamespace(config_path=p))
    assert len(found) == 5
    assert tmp_path/'runtime/raw' in found
    assert tmp_path/'old/demonstration' in found
    assert tmp_path/'data/hil_data/policy_hil' in found
    assert not (tmp_path/'data').exists()
