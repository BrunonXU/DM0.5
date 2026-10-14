import json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import PresetStore

def profile():
    return json.loads((Path(__file__).resolve().parents[1]/'configs/f1-09.json').read_text())

def test_xy_default_preserves_user_presets_and_deletion(tmp_path):
    cfg=profile();cfg['default_presets']=[p for p in cfg['default_presets'] if p['source']=='lxl_xy_reset'];path=tmp_path/'presets.json'
    bare={**cfg,'default_presets':[]}
    old=PresetStore(path,bare).save({'name':'复位零点','joints':{'body_j3':-5}})
    store=PresetStore(path,cfg);rows=store.all()
    assert len(rows)==2 and rows[1]==old
    reset=rows[0]
    assert reset['source']=='lxl_xy_reset' and len(reset['joints'])==14
    assert [reset['joints'][f'arm_l_j{i}'] for i in range(1,8)]==[110,-90,-90,-110,0,0,0]
    assert [reset['joints'][f'arm_r_j{i}'] for i in range(1,8)]==[-110,-90,90,-110,0,0,0]
    assert len(PresetStore(path,cfg).all())==2
    store.delete(reset['id'])
    assert PresetStore(path,cfg).all()==[old]
    # An exported default can be restored explicitly and is a distinct user row.
    restored=store.import_many([reset])[0]
    assert restored['joints']==reset['joints'] and restored['id']!=reset['id']

def test_import_rejects_radians_atomically(tmp_path):
    store=PresetStore(tmp_path/'presets.json',profile());before=store.all()
    with pytest.raises(ValueError,match='degree'):
        store.import_many([{'name':'wrong units','joints':{'arm_l_j1':1.5},'units':'radian'}])
    assert store.all()==before
