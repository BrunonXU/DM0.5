import json
from pathlib import Path
from types import SimpleNamespace
from rollout_catalog import decorate
from data_organization import source_parent, anchors


def row(**binding):
    return dict(source='hil_policy_hil',run_id='run',name='run',path='/immutable/run',
                started_at='2026-10-22T03:36:24+00:00',source_signature='sha',binding=dict(
                    model_id='f1-toys-eef-efe3e5fa54ed0fb2',execution_mode='powered',standalone_rtc=True,**binding))


def events(tmp_path, items):
    p=tmp_path/'journal/events.jsonl';p.parent.mkdir(exist_ok=True)
    p.write_text(''.join(json.dumps(dict(event=k,monotonic_ns=v))+'\n' for k,v in items))
    return p


def test_rollout_duration_excludes_wait_and_preserves_identity(tmp_path):
    p=events(tmp_path,[('runtime_waiting_for_start',1),('local_policy_authorized',10_000_000_000),('session_termination',105_805_000_000)])
    before=p.read_bytes();r=decorate(row(),tmp_path,{})
    assert r['name']=='2026-10-22 11:36:24 · 执行 95.8秒'
    assert source_parent(r)=='真机Rollout/V1_FullSFT_1000'
    assert {'真机Rollout','真机Rollout/V1_FullSFT_1000'} <= anchors([r])
    assert r['path']=='/immutable/run' and r['run_id']=='run' and r['source_signature']=='sha'
    assert before==p.read_bytes()


def test_hil_shadow_unknown_not_misclassified(tmp_path):
    hil=row(rtc_hil=True)
    assert decorate(hil,tmp_path,{})['library_folder']=='HIL/DAgger'
    shadow=row();shadow['binding']['execution_mode']='shadow'
    assert decorate(shadow,tmp_path,{})['library_folder']=='其他录制/Shadow测试'
    unknown=row();unknown['binding'].pop('standalone_rtc')
    assert decorate(unknown,tmp_path,{})['capture_mode']=='unknown'


def test_rlt_launch_wins_over_shared_hil_and_bc_q_phase(tmp_path):
    for phase, updates in [('bc', 0), ('q', 32)]:
        r = row(rtc_hil=True, rlt=dict(control_mode='rlt_hil_supervised',
                                     training_phase=phase, q_guidance_updates=updates))
        result = decorate(r, tmp_path, {})
        assert result['capture_mode'] == 'rlt' and result['library_folder'] == 'HIL/RLT'
        assert result['source'] == 'hil_policy_hil'
        assert result['capture_classification']['training_eligibility'] == 'separate_review_required'
        assert 'training_ready' not in result


def test_manual_launch_and_conflicting_explicit_tag(tmp_path):
    r = row(); r['source'] = 'hil_demonstration'
    assert decorate(r, tmp_path, {})['library_folder'] == 'HIL/人工示教'
    conflict = row(rtc_hil=True, capture_mode='demonstration')
    assert decorate(conflict, tmp_path, {})['capture_mode'] == 'unknown'
    malformed = row(rtc_hil=True, rlt={'unrecognized': True})
    assert decorate(malformed, tmp_path, {})['capture_mode'] == 'unknown'


def test_model_training_name_does_not_define_capture_mode(tmp_path):
    r = row(rtc_hil=True)
    r['binding']['model_id'] = 'trained-with-RLT-and-DAgger'
    assert decorate(r, tmp_path, {})['capture_mode'] == 'dagger'


def test_incomplete_interval_is_unknown_and_cache_tracks_file(tmp_path):
    p=events(tmp_path,[('local_policy_authorized',10_000_000_000)])
    assert decorate(row(),tmp_path,{})['execution_duration_s'] is None
    with p.open('a') as f:f.write(json.dumps(dict(event='session_termination',monotonic_ns=12_000_000_000))+'\n')
    assert decorate(row(),tmp_path,{})['execution_duration_s']==2
    r=row();r['binding']['model_id']='brand-new/model'
    assert decorate(r,tmp_path,{})['library_folder']=='真机Rollout/未登记模型_brand-new_model'


def test_browser_groups_by_checkpoint_and_retains_manual_folders(tmp_path):
    import subprocess, shutil, pytest
    if not shutil.which('node'):
        pytest.skip('node not installed; browser checks run on development host')
    web=Path(__file__).resolve().parents[1]/'web/data-library.js'
    (tmp_path/'library.mjs').write_bytes(web.read_bytes())
    (tmp_path/'test.mjs').write_text('''
import assert from 'assert';import {libraryView} from './library.mjs';
const e={id:'one',name:'2026-10-22 11:36:24 · 执行 95.8秒',source:'hil_policy_hil',path:'/raw/one',source_signature:'sha',library_folder:'真机Rollout/V1_FullSFT_1000'};
const catalog={episodes:[e]};
assert.equal(libraryView(catalog,'真机rollout').folders[0].name,'V1_FullSFT_1000');
assert.equal(libraryView(catalog,'DAgger/策略 HIL').leaves.length,0);
assert.equal(libraryView(catalog,'真机Rollout/V1_FullSFT_1000').leaves[0].name,e.name);
catalog.organization={folders:[{id:'@custom',parent:'真机rollout',name:'精选'}],memberships:{one:{folder:'@custom',source_signature:'sha'}}};
assert.equal(libraryView(catalog,'@custom').leaves[0].path,e.path);
''')
    subprocess.run(['node',str(tmp_path/'test.mjs')],check=True)
