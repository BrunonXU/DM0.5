import json
from pathlib import Path
from recording.unified.jobs import enqueue,scan,run_one,active_capture
from test_unified_capture import row,jpeg
from recording.unified.capture import CaptureWriter
from recording.unified.contract import CAMERAS


def test_queue_idempotence_idle_gate_and_restart(tmp_path):
    (tmp_path/'storage.json').write_text(json.dumps(dict(schema='f1_local_storage_v1',root=str(tmp_path/'data'))))
    w=CaptureWriter(tmp_path/'data/pico_data',dict(capture_id='one',fps=30))
    for i in range(3):w.append(row(i),{k:jpeg() for k in CAMERAS})
    w.close();a=scan(tmp_path);assert len(a)==1 and scan(tmp_path)==a
    assert run_one(tmp_path,'missing','missing',idle=lambda:True)['status']=='paused_capture_active'
    job=json.loads(Path(a[0]).read_text());assert job['attempts']==0
    assert active_capture(tmp_path/'missing_proc') is True


def test_unknown_actions_never_official_supervision(tmp_path):
    from recording.unified.adapters import canonical
    w=CaptureWriter(tmp_path,dict(capture_id='one',fps=30))
    for i in range(3):
        r=row(i);r['action_valid']=[False]*16;r['action']=[None]*16
        w.append(r,{k:jpeg() for k in CAMERAS})
    p=w.close();bundle=canonical(p)
    assert not bundle.segments


def test_dataset_catalog_requires_verified_manifest(tmp_path):
    from recording.unified.catalog import CaptureCatalog
    (tmp_path/'storage.json').write_text(json.dumps(dict(schema='f1_local_storage_v1',root=str(tmp_path/'data'))))
    d=tmp_path/'data/datasets/one';d.mkdir(parents=True)
    audit=dict(structure_verified=False,reader_roundtrip_verified=False,frames=3,episodes=1,fps=30)
    (d/'f1_audit.json').write_text(json.dumps(audit));(d/'f1_files.json').write_text(json.dumps({'files':{}}))
    c=CaptureCatalog(tmp_path);assert c.datasets()==[]
    audit.update(structure_verified=True,reader_roundtrip_verified=True)
    (d/'f1_audit.json').write_text(json.dumps(audit))
    # Structurally verified fixture is catalogued but never human-approved.
    assert c.datasets()[0]['training_ready'] is False
