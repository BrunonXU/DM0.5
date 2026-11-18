"""Pinned overlay registration must precede native construction and fail optional."""
import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
import pytest
from f1_harness.mesh_cache import register_mesh_cache


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    bundle=tmp_path/'bundle';overlay=bundle/'upstream/f1_safety';overlay.mkdir(parents=True)
    base=tmp_path/'infra/vendor/f1_safety/src/python/f1_mesh_collision.py'
    base.parent.mkdir(parents=True);base.write_text('original\n')
    (bundle/'configs').mkdir()
    descriptor=bundle/'configs/collision-cache-overlay.json'
    descriptor.write_text(json.dumps(dict(schema='f1-harness-collision-cache-overlay-v1',base_module_sha256=hashlib.sha256(base.read_bytes()).hexdigest())))
    (overlay/'f1_mesh_asset_cache.py').write_text('VALUE = 3\n')
    (overlay/'f1_mesh_collision.py').write_text('def configure_asset_cache(root, abi):\n global configured\n configured = (root, abi)\n')
    files=[descriptor,*overlay.glob('*.py')]
    (bundle/'manifest.json').write_text(json.dumps(dict(files={str(p.relative_to(bundle)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files})))
    site=tmp_path/'site';site.mkdir()
    (site/'launch.local.json').write_text(json.dumps(dict(runtime=str(tmp_path/'runtime'),infra_root=str(tmp_path/'infra'))))
    cfg=ModuleType('f1dagger.config');cfg.pico_paths=lambda root:root
    monkeypatch.setitem(sys.modules,'f1dagger',ModuleType('f1dagger'))
    monkeypatch.setitem(sys.modules,'f1dagger.config',cfg)
    coal=ModuleType('coal');coal.__file__=str(tmp_path/'coal/__init__.py');coal.__version__='fixture'
    Path(coal.__file__).parent.mkdir();(Path(coal.__file__).parent/'native.so').write_bytes(b'fixture binary')
    monkeypatch.setitem(sys.modules,'coal',coal)
    monkeypatch.setitem(sys.modules,'mujoco',SimpleNamespace(__version__='fixture'))
    for name in ('f1_mesh_asset_cache','f1_mesh_collision'):monkeypatch.delitem(sys.modules,name,raising=False)
    yield bundle,site,base,overlay
    for name in ('f1_mesh_asset_cache','f1_mesh_collision'):sys.modules.pop(name,None)


def test_registers_bound_module_before_use(fixture):
    bundle,site,_,overlay=fixture
    result=register_mesh_cache(site,bundle=bundle)
    assert result['enabled'] and result['motion_authorized'] is False
    module=sys.modules['f1_mesh_collision']
    assert Path(module.__file__)==overlay/'f1_mesh_collision.py'
    assert module.configured[0]==site.parent/'runtime/static-geometry-cache'
    # A second registration cannot replace an already used module.
    assert not register_mesh_cache(site,bundle=bundle)['enabled']
    assert sys.modules['f1_mesh_collision'] is module


@pytest.mark.parametrize('change',['base','overlay','manifest','missing','import_error'])
def test_unsupported_overlay_leaves_original_import_route(fixture,change):
    bundle,site,base,overlay=fixture
    if change=='base':base.write_text('different')
    elif change=='overlay':(overlay/'f1_mesh_collision.py').write_text('different')
    elif change=='manifest':(bundle/'manifest.json').write_text('{}')
    elif change=='missing':(site/'launch.local.json').unlink()
    else:
        p=overlay/'f1_mesh_collision.py';p.write_text('raise RuntimeError("fixture constructor registration failure")')
        m=json.loads((bundle/'manifest.json').read_text());m['files'][str(p.relative_to(bundle))]=hashlib.sha256(p.read_bytes()).hexdigest()
        (bundle/'manifest.json').write_text(json.dumps(m))
    assert register_mesh_cache(site,bundle=bundle)['enabled'] is False
    assert 'f1_mesh_collision' not in sys.modules
    assert 'f1_mesh_asset_cache' not in sys.modules
