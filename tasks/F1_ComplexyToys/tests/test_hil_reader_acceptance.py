"""Synthetic, explicitly labelled raw->review->EEF->actual reader acceptance."""
from dataclasses import replace
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from PIL import Image

TASK=Path(__file__).resolve().parents[1]
PROJECTS=TASK.parents[1]
sys.path[:0]=[str(TASK/'scripts'),str(PROJECTS/'DAgger/F1/src'),str(PROJECTS/'DAgger/shared/src')]
from prepare_hil_correction import prepare
from f1dagger.observation import Observation
from dagger_core.recording_observation import TimestampedFrame, DeviceTimestamp
from f1dagger.recording import F1Recorder, make_sink, audit


@pytest.mark.parametrize('right_only_anchor_cap', [False, True])
def test_reviewed_correction_preserves_error_state_masks_and_real_hold(tmp_path, monkeypatch, right_only_anchor_cap):
    monkeypatch.setenv('F1_TOYS_TOKENIZER',str(TASK/'artifacts/20261020-v1-full-training/bundle-250/paligemma_tokenizer.model'))
    raw=tmp_path/'raw';raw.mkdir()
    journal=F1Recorder(raw/'journal',episode_id='synthetic-hil',task_prompt='test',
        binding=dict(execution_mode='powered',recording_mode='hil-record',purpose='policy_hil',synthetic=True))
    sink=make_sink(raw/'payload','hil-record')
    payload={}
    for c,size in [('head',(1280,720)),('left_wrist',(640,480)),('right_wrist',(640,480))]:
        out=BytesIO();Image.new('RGB',size,(100,120,140)).save(out,format='JPEG');payload[c]=(out.getvalue(),size)
    t0=1_000_000_000;begin=t0+300_000_000;end=t0+4_200_000_000;hold_start=t0+2_000_000_000
    for i in range(421):
        t=t0+i*10_000_000
        human=t>=begin
        source='human_hold' if t>=hold_start else 'human_arm' if human else 'policy'
        command=np.zeros(16);command[7]=.2 if human else 99.
        journal.control(now=t-1,state='hil_latched' if human else 'policy',intervention=int(human),
            epoch=1 if human else 0,selected=command,submitted=command,source=source,
            measured=np.r_[np.zeros(7),4.,np.zeros(8)],sequence=i+1,
            publication_complete=True,execution_mode='powered',action_source_timestamps_ns=[t-2]*16,
            publications=[dict(topic_key=k,monotonic_ns=t-2,publisher_gid='aa'*16)
                          for k in ('joint_command','left_gripper_command','right_gripper_command')])
    for i in range(127):
        t=t0+round(i/30*1e9);human=t>=begin
        frames=tuple(TimestampedFrame(c,i,DeviceTimestamp(t,'ns','synthetic'),t,data,'image/jpeg',*size)
                     for c,(data,size) in payload.items())
        obs=Observation(i,t,t+10,frames,tuple(np.r_[np.zeros(7),4.,np.zeros(8)]),(t,)*16,(0.,)*22)
        assert sink.submit(obs)
        assert journal.record_timestamped_observation(obs,hil_state='hil_latched' if human else 'policy',
                                                     intervention_id=int(human),policy_epoch=1 if human else 0)
    assert sink.close() and journal.close()
    check=audit(raw);assert check['valid']
    review=dict(contract='f1_upper_16d_v1',manifest_sha256=check['manifest_sha256'],
        human_review_accepted=True,reviewer='SYNTHETIC TEST ONLY',accepted_interventions=[1],
        accepted_time_ranges_ns=[[begin,end]],dataset_role='correction',source_group='synthetic-session',
        split='validation',task_success=False,environment_assistance_ranges_ns=[],
        phase_ranges=[dict(stage='synthetic_recovery',start_ns=begin,end_ns=end)],
        terminal_hold_range_ns=[hold_start,end],terminal_stable_confirmed=True)
    anchor_end = hold_start+500_000_000
    if right_only_anchor_cap:
        review['anchor_time_ranges_ns'] = [[begin, anchor_end]]
    review_path=tmp_path/'review.json';review_path.write_text(json.dumps(review))
    output=tmp_path/'reader'
    receipt=prepare(raw,review_path,TASK/'artifacts/20261020-home-time-mask/dataset/train',output,
        contract_path=TASK/'configs/eef_v1_dagger_right_only.json' if right_only_anchor_cap else None)
    assert receipt['rlinf_reader_verified']
    assert 0 < receipt['reader_samples'] < 96 if right_only_anchor_cap else receipt['reader_samples']>96
    assert receipt['synthetic'] and not receipt['human_review_accepted']
    assert receipt['terminal_hold']['real_label_steps']>=60
    with np.load(output/'arrays.npz') as arrays:
        mask=arrays['action_time_mask']
        assert mask.sum(1).max()==50
        if not right_only_anchor_cap:
            assert mask.sum(1).min()==1
        assert (arrays['states'][:,7]==4.).all()  # Actual error state retained.
        assert np.allclose(arrays['submitted'][mask][:,7],.2)  # No model99 labels or measured4 labels.
        assert (arrays['actions'][~mask]==0).all()
    manifest=json.loads((output/'manifest.json').read_text())
    assert not manifest['formal_training_ready'] and 'policy' not in manifest['source_counts']
    if right_only_anchor_cap:
        rows = [json.loads(line) for line in (output/'samples.jsonl').open()]
        assert all(r['anchor_monotonic_ns'] <= anchor_end for r in rows)
        assert max(t for r in rows for t in r['action_grid_ns']) > anchor_end+1_000_000_000
        assert all(set(r['images']) == {'head','right_wrist'} for r in rows)
        assert receipt['anchor_filtered_windows'] > 0
    assert audit(raw)['manifest_sha256']==check['manifest_sha256']
