"""Legacy/new raw roots stay discoverable without counting derived exports as takes."""
from pathlib import Path
import shutil
import subprocess
import pytest


def test_recording_and_export_navigation(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    (tmp_path/'library.mjs').write_text((Path(__file__).resolve().parents[1]/'web/data-library.js').read_text())
    (tmp_path/'test.mjs').write_text('''
import assert from 'assert';
import {libraryView,libraryPage} from './library.mjs';
const old={id:'old',path:'/runtime/raw/old',name:'old',source:'hil_demonstration',size_bytes:100,started_at:'2026-10-10'};
const recent={id:'new',path:'/data/hil_data/demonstration/new',name:'new',source:'hil_demonstration',size_bytes:50,started_at:'2026-10-13'};
const dataset={id:'v3',path:'/data/datasets/v3',name:'v3',source:'unified_dataset',size_bytes:20,dataset_episodes:74};
const pico={id:'pico',path:'/lxl/pico_data/all_data/ep',relative_path:'all_data/ep',name:'ep',source:'lxl'};
const catalog={episodes:[old,recent,dataset,pico,old]};
const root=libraryView(catalog);
assert.strictEqual(root.records.length,3);assert.strictEqual(root.exports.length,1);
assert.deepStrictEqual(libraryView(catalog,'DAgger').folders.map(f=>[f.name,f.count,f.bytes]),[['人工示教',2,150],['策略 HIL',0,0]]);
assert.deepStrictEqual(libraryView(catalog,'DAgger/人工示教').leaves.map(e=>e.id),['new','old']);
assert.strictEqual(libraryView(catalog,'home/user/Bruno').current,'');
assert.strictEqual(libraryView(catalog,'LXL/all_data').leaves[0].path,pico.path);
assert.strictEqual(libraryView(null).records.length,0);
assert.strictEqual(libraryView({episodes:[]},'DAgger/策略 HIL').leaves.length,0);
const policy={...recent,id:'hil',name:'hil',path:'/data/hil_data/policy_hil/hil',source:'hil_policy_hil'};
assert.strictEqual(libraryView({episodes:[policy]},'DAgger/策略 HIL').leaves[0].id,'hil');
// Large catalogues stay bounded; source filters include compatible old roots.
const many=Array.from({length:137},(_,i)=>({...recent,id:'record-'+i,name:'take-'+String(i).padStart(3,'0'),path:'/raw/'+i,started_at:'2026-10-13T'+String(Math.floor(i/60)).padStart(2,'0')+':'+String(i%60).padStart(2,'0')+':00Z',review:{status:i%2?'approved':'pending'}}));
const large=libraryView({episodes:[...many,pico,dataset]});
const first=libraryPage(large);
assert.strictEqual(first.rows.length,12);assert.strictEqual(first.total,138);assert.strictEqual(first.pages,12);
const last=libraryPage(large,{page:1000});
assert.strictEqual(last.page,12);assert.strictEqual(last.rows.length,6);
assert.strictEqual(libraryPage(large,{review:'approved'}).total,68);
assert.strictEqual(libraryPage(large,{query:'TAKE-13',review:'pending'}).total,4);
assert.strictEqual(libraryPage(large,{query:'no-match'}).pages,1);
assert.strictEqual(libraryPage(large,{query:'no-match'}).start,0);
assert.strictEqual(libraryPage(large,{kind:'exports'}).rows[0].id,'v3');
assert.strictEqual(libraryPage(libraryView({episodes:[...many,pico]},'LXL')).total,1);
assert.strictEqual(libraryPage(large,{pageSize:1000}).rows.length,100);
assert.deepStrictEqual(libraryPage(large,{page:2}).rows.filter(e=>first.rows.some(f=>f.id===e.id)),[]);
''')
    subprocess.run([node, str(tmp_path/'test.mjs')], check=True, capture_output=True, text=True, timeout=10)
