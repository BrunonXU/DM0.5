"""Optional geometry cache cannot reuse wrong/corrupt shapes or hide build errors."""
from pathlib import Path
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pytest

source = Path(__file__).resolve().parents[2] / 'F1-4242/integrations/pico_upper/vendor/f1_safety/src/python/f1_mesh_asset_cache.py'
spec = importlib.util.spec_from_file_location('_tested_mesh_asset_cache', source)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
MeshAssetCache = module.MeshAssetCache


class Shape:
    loads = 0
    def __init__(self, value=0): self.value = value
    def saveToBinary(self, path): Path(path).write_text(str(self.value))
    def loadFromBinary(self, path):
        type(self).loads += 1
        self.value = int(Path(path).read_text())


vertices = np.array([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.]])
faces = np.array([[0,1,2]])


def cache(tmp_path, abi=None):
    return MeshAssetCache(tmp_path, abi or {'library': 'fixture-v1'}, (Shape, Shape))


def build(): return Shape(17), Shape(23)


def test_hit_restores_distinct_objects_without_rebuilding(tmp_path):
    c = cache(tmp_path)
    first = c.get_or_build(vertices, faces, build)
    second = c.get_or_build(vertices, faces, lambda: pytest.fail('rebuilt on hit'))
    assert [x.value for x in second] == [17,23]
    assert all(a is not b for a,b in zip(first,second))
    second[0].value = -9
    assert c.get_or_build(vertices, faces, build)[0].value == 17
    assert c.stats == dict(hit=2,miss=1,read_error=0,write_error=0)


@pytest.mark.parametrize('changed', ['vertices','faces','abi'])
def test_source_and_binary_abi_are_part_of_identity(tmp_path, changed):
    c=cache(tmp_path);c.get_or_build(vertices,faces,build)
    v=vertices.copy();f=faces.copy()
    if changed=='vertices':v[0,0]=.001
    elif changed=='faces':f[0]=[0,2,1]
    else:c=cache(tmp_path,{'library':'fixture-v2'})
    assert c.get_or_build(v,f,lambda:(Shape(31),Shape(32)))[0].value==31


@pytest.mark.parametrize('damage', ['truncated','marker','identity','symlink','missing','native_error'])
def test_bad_cache_rebuilds_from_original_and_repairs(tmp_path, damage):
    c=cache(tmp_path);c.get_or_build(vertices,faces,build)
    directory=tmp_path/c.identity(vertices,faces);marker=directory/'entry.json'
    entry=json.loads(marker.read_text());path=directory/entry['files'][1]['name']
    Shape.loads=0
    if damage=='truncated':path.write_text('')
    elif damage=='missing':path.unlink()
    elif damage=='symlink':path.unlink();path.symlink_to(tmp_path/'elsewhere')
    elif damage=='marker':marker.write_text('{bad')
    elif damage=='identity':entry['identity']='different';marker.write_text(json.dumps(entry))
    else:
        path.write_text('invalid-native-format');entry['files'][1]['sha256']=module.file_hash(path)
        marker.write_text(json.dumps(entry))
    assert c.get_or_build(vertices,faces,build)[1].value==23
    if damage!='native_error':assert Shape.loads==0
    assert c.stats['read_error']==1
    assert c.get_or_build(vertices,faces,lambda:pytest.fail('not repaired'))[0].value==17


def test_cache_write_failure_does_not_fail_the_original_constructor(tmp_path):
    root=tmp_path/'not-a-directory';root.write_text('occupied')
    c=cache(root)
    assert c.get_or_build(vertices,faces,build)[0].value==17
    assert c.stats['write_error']==1
    def broken():raise RuntimeError('original mesh failure')
    with pytest.raises(RuntimeError,match='original mesh failure'):
        c.get_or_build(vertices,faces,broken)


def test_parallel_generation_is_atomic_and_readers_are_independent(tmp_path):
    def call(_):return cache(tmp_path).get_or_build(vertices,faces,build)
    with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(call,range(24)))
    assert all([x.value for x in result]==[17,23] for result in results)
    assert len({id(result[0]) for result in results})==24
    assert cache(tmp_path).get_or_build(vertices,faces,lambda:pytest.fail('cache lost'))[0].value==17


def test_redirected_root_uses_original_build_without_writing_elsewhere(tmp_path):
    elsewhere=tmp_path/'real';elsewhere.mkdir()
    root=tmp_path/'linked';root.symlink_to(elsewhere,target_is_directory=True)
    c=cache(root)
    assert c.get_or_build(vertices,faces,build)[0].value==17
    assert not list(elsewhere.iterdir())
