"""Storage counts must not duplicate exported episodes or invent free space."""
from pathlib import Path
import shutil
import subprocess
import pytest


def test_storage_accounting(tmp_path):
    node=shutil.which('node')
    if not node:pytest.skip('node not installed')
    module=tmp_path/'storage.mjs'
    module.write_text((Path(__file__).resolve().parents[1]/'web/data-storage.js').read_text())
    test=tmp_path/'test.mjs'
    test.write_text('''
import assert from 'assert';
import {storageSummary,storageHTML} from './storage.mjs';
const rows=[{path:'/a',source:'lxl',size_bytes:9e9},{path:'/b',source:'hil_demonstration',size_bytes:2e9},{path:'/c',source:'hil_policy_hil',size_bytes:1e9},{path:'/d',source:'unified_dataset',size_bytes:5e9,dataset_episodes:74},{path:'/a',source:'lxl',size_bytes:9e9}];
const disk={total_bytes:100e9,used_bytes:20e9,free_bytes:75e9};
const s=storageSummary({episodes:rows},disk);
assert.deepStrictEqual(s.categories.map(c=>[c.bytes,c.count]),[[9e9,1],[3e9,2]]);
assert.strictEqual(s.total,12e9);assert.strictEqual(s.count,3);assert.strictEqual(s.capacity.reserved,5e9);
assert.strictEqual(storageSummary({episodes:[]},null).capacity,null);
assert.strictEqual(storageSummary(null,disk).available,false);
assert.strictEqual(storageSummary({episodes:[]}, {...disk,free_bytes:101e9}).capacity,null);
assert.strictEqual(storageSummary({episodes:rows,error:'unreadable'},disk).partial,true);
assert.strictEqual(storageSummary({episodes:[{path:'/a',source:'lxl',size_bytes:-1}]},disk).partial,true);
const empty=storageHTML({episodes:[]},null);assert(!empty.includes('NaN'));assert(!empty.includes('Infinity'));assert(empty.includes('0 条'));
assert(storageHTML({episodes:rows},disk,{stale:true}).includes('上次读取'));
assert(storageHTML({episodes:rows},disk).includes('75.00 GB'));
assert.strictEqual(storageSummary({episodes:[{path:'/p',source:'pico_capture',size_bytes:1}]},disk).categories[0].count,1);
''')
    subprocess.run([node,str(test)],check=True,capture_output=True,text=True,timeout=10)
