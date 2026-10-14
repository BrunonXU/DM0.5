import copy
from io import BytesIO
import json
from pathlib import Path
import pytest
from PIL import Image
from recording.unified.capture import CaptureWriter, read_capture, read_image, commit_capture
from recording.unified.contract import CAMERAS, CONTRACT, validate_row
from recording.unified.selection import segments
from recording.unified.pico import RawCapture, RawDataset, KEYS


def jpeg():
    f=BytesIO();Image.new('RGB',(64,48),(120,90,20)).save(f,format='JPEG');return f.getvalue()


def row(i=0, **kwargs):
    ns=1_000_000_000+i*33_333_333
    return dict(contract=CONTRACT,time_ns=ns,session_id='session',clock_basis='host_monotonic_receipt',
        state=[1.0]*16,action=[2.0]*16,action_valid=[True]*16,
        action_source='verified_pico_session_and_graph',control_source='pico_human',phase='human',
        execution_mode='powered',owner_epoch=1,intervention_id=0,state_max_age_ns=1,arm_action_max_age_ns=1,
        images={k:dict(frame_id=i,received_ns=ns,width=64,height=48) for k in CAMERAS},**kwargs)


def test_capture_originals_and_staged_commit(tmp_path):
    w=CaptureWriter(tmp_path,dict(capture_id='one',fps=30));payload=jpeg()
    for i in range(4):w.append(row(i),{k:payload for k in CAMERAS})
    w.append_extra('head',dict(frame_id=5),payload)
    staging=w.close(commit=False)
    with pytest.raises(ValueError):read_capture(staging)
    final=commit_capture(staging,tmp_path/'one')
    meta,rows=read_capture(final)
    assert meta['rows']==4 and not meta['training_ready']
    assert read_image(final,rows[1]['images']['head'])==payload
    with (final/'head.pack').open('r+b') as f:f.write(b'bad')
    with pytest.raises(ValueError,match='hash'):read_capture(final)


def test_malformed_action_mask_and_unknown_actions(tmp_path):
    r=row();r['action']=[None]*16;r['action_valid']=[]
    with pytest.raises(ValueError,match='validity'):validate_row(r)
    r['action_valid']=[False]*16
    assert validate_row(r)
    assert not segments([r])[0]


def test_training_boundaries_and_freshness_never_inferred():
    rows=[row(i) for i in range(10)]
    rows[4]['phase']='home'
    groups,quality=segments(rows)
    assert [len(g) for g in groups]==[4,5]
    rows=[row(i) for i in range(3)]
    for r in rows:r.pop('state_max_age_ns')
    assert not segments(rows)[0]
    rows=[row(i) for i in range(3)];rows[1]['images']['head']['frame_id']=0
    assert not segments(rows)[0]


def test_no_encoder_raw_pico_stop_save(tmp_path,monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('inline encoder forbidden'))
    config=dict(recording_session_id='session',task='place wooden block',fps=30,
        source_basis='verified_pico_session_and_graph',pico_session_identity='proof',
        pico_source_proof={'verified':True},pico_source_epoch=1)
    capture=RawCapture(tmp_path,config,{k:dict(width=64,height=48) for k in KEYS})
    for i in range(5):
        ns=1_000_000_000+i*33_333_333
        timing=dict(camera_arrival_timestamp_ns=[ns]*3,camera_frame_ids=[i]*3,
                    camera_original_timestamp_ns=[ns+999]*3,state_max_age_ns=1,action_max_age_ns=2)
        capture.add([1.]*16,[2.]*16,ns,{k:(jpeg(),ns) for k in KEYS},timing)
    arrays,media,timing=capture.stop()
    assert timing['raw_save_allowed'] and not timing['training_ready']
    _,saved=RawDataset(tmp_path).commit(capture.path,arrays,media)
    meta,rows=read_capture(saved)
    assert meta['collection_method']=='pico' and len(rows)==5
    assert rows[0]['images']['head']['original_timestamp_ns']==1_000_000_999
    assert len(segments(rows)[0])==1


def test_common_catalog_and_replay_reads_only_committed(tmp_path):
    from recording.unified.catalog import CaptureCatalog
    (tmp_path/'storage.json').write_text(json.dumps(dict(schema='f1_local_storage_v1',root=str(tmp_path/'data'))))
    w=CaptureWriter(tmp_path/'data/pico_data',dict(capture_id='one',fps=30))
    for i in range(3):w.append(row(i),{k:jpeg() for k in CAMERAS})
    catalog=CaptureCatalog(tmp_path)
    assert catalog.catalog()==[]
    final=w.close();eid=catalog.catalog()[0]['id']
    assert catalog.timeline(eid)['frames']==3
    assert catalog.frame(eid,'head',.04)[1]['observation_id']==1
    with pytest.raises(ValueError):catalog.frame(eid,'../head',0)
