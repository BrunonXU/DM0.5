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
import {libraryView,folderPage,rltAssessment} from './library.mjs';
const old={id:'old',path:'/runtime/raw/old',name:'old',source:'hil_demonstration',size_bytes:100,started_at:'2026-10-10'};
const recent={id:'new',path:'/data/hil_data/demonstration/new',name:'new',source:'hil_demonstration',size_bytes:50,started_at:'2026-10-13'};
const dataset={id:'v3',path:'/data/datasets/v3',name:'v3',source:'unified_dataset',size_bytes:20,dataset_episodes:74};
const pico={id:'pico',path:'/lxl/pico_data/all_data/ep',relative_path:'all_data/ep',name:'ep',source:'lxl'};
const catalog={episodes:[old,recent,dataset,pico,old]};
const root=libraryView(catalog);
assert.strictEqual(root.records.length,3);assert.strictEqual(root.exports.length,1);
assert.deepStrictEqual(libraryView(catalog,'HIL').folders.map(f=>[f.name,f.count,f.bytes]),[['人工示教',2,150],['DAgger',0,0],['RLT',0,0]]);
assert.deepStrictEqual(libraryView(catalog,'DAgger/人工示教').leaves.map(e=>e.id),['new','old']);
assert.strictEqual(libraryView(catalog,'home/user/Bruno').current,'');
assert.strictEqual(libraryView(catalog,'LXL/all_data').leaves[0].path,pico.path);
assert.strictEqual(libraryView(null).records.length,0);
assert.strictEqual(libraryView({episodes:[]},'DAgger/策略 HIL').leaves.length,0);
const policy={...recent,id:'hil',name:'hil',path:'/data/hil_data/policy_hil/hil',source:'hil_policy_hil'};
assert.strictEqual(libraryView({episodes:[policy]},'DAgger/策略 HIL').leaves[0].id,'hil');
const annotated={...recent,review:{status:'pending',notes:'桶向右移'}};
assert.strictEqual(folderPage(libraryView({episodes:[annotated]},'DAgger/人工示教'),{query:'桶向右移'}).total,1);
annotated.review.stale=true;
assert.strictEqual(folderPage(libraryView({episodes:[annotated]},'DAgger/人工示教'),{query:'桶向右移'}).total,0);
// Preserve folder navigation while bounding both recordings and directory cards.
const many=Array.from({length:137},(_,i)=>({...recent,id:'record-'+i,name:'take-'+String(i).padStart(3,'0'),path:'/raw/'+i,started_at:'2026-10-13T'+String(Math.floor(i/60)).padStart(2,'0')+':'+String(i%60).padStart(2,'0')+':00Z'}));
const large=libraryView({episodes:[...many,pico,dataset]},'DAgger/人工示教');
const first=folderPage(large);
assert.strictEqual(first.leaves.length,8);assert.strictEqual(first.total,137);assert.strictEqual(first.pages,18);
const last=folderPage(large,{page:1000});
assert.strictEqual(last.page,18);assert.strictEqual(last.leaves.length,1);
assert.strictEqual(folderPage(large,{query:'TAKE-13'}).total,7);
assert.strictEqual(folderPage(large,{query:'no-match'}).pages,1);
assert.strictEqual(folderPage(large,{query:'no-match'}).start,0);
assert.strictEqual(folderPage(libraryView({episodes:[...many,pico]})).folders.length,3);
assert.strictEqual(folderPage(libraryView({episodes:[...many,pico]}),{query:'take-13'}).total,7);
assert.strictEqual(folderPage(large,{pageSize:1000}).leaves.length,100);
assert.deepStrictEqual(folderPage(large,{page:2}).leaves.filter(e=>first.leaves.some(f=>f.id===e.id)),[]);
const branches=libraryView({episodes:Array.from({length:35},(_,i)=>({...pico,id:'branch-'+i,path:'/branch/'+i,relative_path:'folder-'+i+'/ep'}))},'LXL');
assert.strictEqual(folderPage(branches).folders.length,8);assert.strictEqual(folderPage(branches).total,35);

// Stable logical IDs survive rename/move; raw identity and source scope are preserved.
const organized={episodes:[{...recent,source_signature:'s'},pico,dataset],organization:{folders:[{id:'@a',parent:'DAgger/人工示教',name:'V1'},{id:'@b',parent:'@a',name:'补录'},{id:'@c',parent:'LXL',name:'V1'}],memberships:{new:{folder:'@b',source_signature:'s'}}}};
assert.strictEqual(libraryView(organized,'@b').leaves[0].id,'new');
assert.strictEqual(libraryView(organized,'DAgger/人工示教').leaves.length,0);
assert.strictEqual(folderPage(libraryView(organized,'@a'),{query:'new'}).total,1);
assert.strictEqual(folderPage(libraryView(organized,'@c'),{query:'new'}).total,0);
organized.organization.folders[0].name='V1_验收';organized.organization.folders[0].parent='LXL';
assert.deepStrictEqual(libraryView(organized,'@b').breadcrumbs.map(n=>n.name),['LXL','V1_验收','补录']);
assert.strictEqual(libraryView(organized,'@b').leaves[0].path,recent.path);
assert.strictEqual(libraryView(organized,'@b').exports.length,1);
organized.episodes[0].source_signature='changed';
assert.strictEqual(libraryView(organized,'@b').leaves.length,0);
assert.strictEqual(libraryView(organized,'DAgger/人工示教').leaves[0].id,'new');
assert.strictEqual(libraryView({...organized,episodes:[]},'@b').current,'@b');

// A parent folder's total includes descendants; the explicit all view reveals
// them without moving memberships or counting server archives as local takes.
const local={...recent,id:'direct',path:'/raw/direct',source:'hil_policy_hil',capture_mode:'dagger',source_signature:'s'};
const nested={...local,id:'nested',path:'/raw/nested'};
const archive={id:'archive',library_folder:'HIL/DAgger',capture_mode:'dagger'};
const grouped={episodes:[local,nested,dataset],archives:[archive],organization:{folders:[{id:'@corrections',parent:'HIL/DAgger',name:'HIL纠正'}],memberships:{nested:{folder:'@corrections',source_signature:'s'}}}};
const parent=libraryView(grouped,'HIL/DAgger');
assert.strictEqual(parent.scoped.length,2);assert.strictEqual(parent.leaves.length,1);assert.strictEqual(parent.archives.length,1);
assert.strictEqual(folderPage(parent).leaves.length,1);
assert.deepStrictEqual(folderPage(parent,{browseMode:'all'}).matching.map(e=>e.id),['direct','nested']);
assert.strictEqual(folderPage(parent,{browseMode:'all'}).folders.length,0);
assert.strictEqual(parent.records.length,2);assert.strictEqual(parent.exports.length,1);
assert.strictEqual(libraryView(grouped,'HIL/RLT').archives.length,0);
assert.strictEqual(libraryView(grouped,'@corrections').archives.length,0);
assert.strictEqual(libraryView({...grouped,archives_error:'unreadable'},'HIL/DAgger').archivesError,'unreadable');

// The time view is global even after entering a nested source folder. Source
// filtering follows capture identity, not a user-created folder or model name.
const evaluation={...local,id:'evaluation',path:'/raw/evaluation',capture_mode:'rollout',library_folder:'真机Rollout/DAgger_model',started_at:'2026-10-29T08:00:00Z'};
const globalCatalog={...grouped,episodes:[nested,dataset,evaluation,local]};
const nestedView=libraryView(globalCatalog,'@corrections');
assert.deepStrictEqual(folderPage(nestedView,{browseMode:'time'}).matching.map(e=>e.id),['evaluation','direct','nested']);
assert.strictEqual(folderPage(nestedView,{browseMode:'time'}).folders.length,0);
assert.deepStrictEqual(folderPage(nestedView,{browseMode:'time',sourceMode:'dagger'}).matching.map(e=>e.id),['direct','nested']);
assert.deepStrictEqual(folderPage(nestedView,{browseMode:'time',sourceMode:'rollout'}).matching.map(e=>e.id),['evaluation']);
assert.deepStrictEqual(folderPage(nestedView,{browseMode:'folders'}).matching.map(e=>e.id),['nested']);
assert.strictEqual(folderPage(nestedView,{browseMode:'time',sourceMode:'demonstration'}).total,0);
const firstGlobal=folderPage(nestedView,{browseMode:'time',pageSize:2});
assert.deepStrictEqual(firstGlobal.leaves.map(e=>e.id),['evaluation','direct']);
assert.deepStrictEqual(folderPage(nestedView,{browseMode:'time',pageSize:2,page:2}).leaves.map(e=>e.id),['nested']);

// No success or training conclusion can come from a human-editable note.
const rlt={...local,id:'rlt',path:'/raw/rlt',capture_mode:'rlt',library_folder:'HIL/RLT',review:{notes:'成功，已训练',status:'approved'}};
assert.strictEqual(rltAssessment(rlt).split,'pending');
assert.strictEqual(rltAssessment(local),null);
rlt.rlt_assessment={source_signature:'s',split:'train',outcome_label:'人工辅助成功',confirmation_kind:'manual_confirmation'};
assert.strictEqual(rltAssessment(rlt).split,'train');
assert.strictEqual(folderPage(libraryView({episodes:[rlt]},'HIL/RLT'),{assessmentFilter:'manual_confirmation'}).matching.length,1);
rlt.rlt_assessment.source_signature='stale';
assert.strictEqual(rltAssessment(rlt).split,'pending');
assert.strictEqual(folderPage(libraryView({episodes:[rlt]},'HIL/RLT'),{assessmentFilter:'pending'}).matching.length,1);
assert.strictEqual(folderPage(libraryView({episodes:[rlt]},'HIL/RLT'),{assessmentFilter:'manual_confirmation'}).matching.length,0);
rlt.rlt_assessment={source_signature:'s',split:'exclude',quality:'missing_control'};
assert.strictEqual(rltAssessment(rlt).split,'excluded');
''')
    subprocess.run([node, str(tmp_path/'test.mjs')], check=True, capture_output=True, text=True, timeout=10)
