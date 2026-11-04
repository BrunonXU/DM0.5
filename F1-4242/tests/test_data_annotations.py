"""Notes survive moving a recording without the workbench runtime directory."""
import hashlib
import json
import shutil
import pytest
import data_annotations as annotations
from data_service import DataService
from test_data_auth import episode


def make_store(tmp_path):
    raw=episode(tmp_path/'raw')
    service=DataService(tmp_path/'raw',tmp_path/'runtime',tmp_path/'web')
    return raw,service,service.catalog()['episodes'][0]


def test_saved_notes_travel_without_browser_or_runtime(tmp_path):
    raw,service,row=make_store(tmp_path)
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.iterdir()}
    saved=service.save_review(row['id'],'approved','布局 A\n[2.0s] 释放','tester',row['source_signature'],'none')
    document=json.loads((raw/annotations.NAME).read_text())
    assert document['review']['notes']==saved['notes']
    assert service.episode(row['id'],True)['source_signature']==row['source_signature']
    assert before=={n:hashlib.sha256((raw/n).read_bytes()).hexdigest() for n in before}
    copied=tmp_path/'another-machine'/'renamed';shutil.copytree(raw,copied,copy_function=shutil.copyfile)
    other=DataService(copied.parent,tmp_path/'brand-new-runtime',tmp_path/'web')
    moved=other.catalog()['episodes'][0]
    assert moved['id']!=row['id'] and moved['source_signature']!=row['source_signature']
    assert moved['review']['notes']==saved['notes']
    assert moved['review']['status']=='pending' and moved['review']['relocated']
    assert moved['review']['previous_review']['status']=='approved'
    updated=other.save_notes(moved['id'],'copied note edited','editor',moved['source_signature'],moved['review']['revision'])
    assert updated['notes']=='copied note edited'
    assert DataService(copied.parent,tmp_path/'third-runtime',tmp_path/'web').catalog()['episodes'][0]['review']['notes']==updated['notes']
    assert service.episode(row['id'],True)['review']['notes']==saved['notes']


def test_legacy_migration_preserves_revision_and_never_overwrites(tmp_path):
    raw,service,row=make_store(tmp_path)
    review=dict(episode_id=row['id'],status='approved',notes='legacy',source_signature=row['source_signature'],updated_at='old',reviewer='human')
    service.review_root.mkdir(parents=True);legacy=service.review_root/(row['id']+'.json');legacy.write_text(json.dumps(review))
    old=legacy.read_bytes();prior=service.episode(row['id'],True)['review']
    annotations.save(row,review,create_only=True)
    assert service.episode(row['id'],True)['review']==prior and legacy.read_bytes()==old
    with pytest.raises(ValueError,match='覆盖'):annotations.save(row,{**review,'notes':'another'},create_only=True)
    cleared=service.save_notes(row['id'],'','tester',row['source_signature'],prior['revision'])
    assert cleared['notes']=='' and legacy.read_bytes()==old
    assert DataService(service.root,tmp_path/'empty-runtime',tmp_path/'web').catalog()['episodes'][0]['review']['notes']==''


def test_unwritable_sidecar_does_not_report_saved_or_fall_back_to_cache(tmp_path,monkeypatch):
    raw,service,row=make_store(tmp_path)
    def fail(*args):raise OSError('disk full')
    monkeypatch.setattr(annotations,'atomic_json',fail)
    with pytest.raises(OSError,match='disk full'):service.save_notes(row['id'],'not saved','tester',row['source_signature'],'none')
    assert not (raw/annotations.NAME).exists() and not service.review_root.exists()


@pytest.mark.parametrize('kind',['corrupt','symlink','wrong_schema'])
def test_invalid_sidecar_is_preserved_and_cannot_be_overwritten(tmp_path,kind):
    raw,service,row=make_store(tmp_path);sidecar=raw/annotations.NAME
    if kind=='symlink':
        target=tmp_path/'outside';target.write_text('keep');sidecar.symlink_to(target)
    else:sidecar.write_text('{broken' if kind=='corrupt' else '{"schema":"unrelated"}')
    before=sidecar.read_bytes();current=service.episode(row['id'],True)
    assert current['review']['annotation_error']
    with pytest.raises(ValueError):service.save_notes(row['id'],'overwrite','tester',row['source_signature'],current['review']['revision'])
    assert sidecar.read_bytes()==before


def test_two_workbenches_cannot_overwrite_same_recording_revision(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from data_service import ReviewConflict
    raw,first,row=make_store(tmp_path)
    second=DataService(first.root,tmp_path/'other-runtime',tmp_path/'web')
    def write(pair):
        store,note=pair
        try:
            store.save_notes(row['id'],note,'editor',row['source_signature'],'none')
            return 'saved'
        except ReviewConflict:
            return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(write,[(first,'one'),(second,'two')]))==['conflict','saved']


def test_capture_tag_only_preserves_missing_review_and_later_notes(tmp_path):
    raw, service, row = make_store(tmp_path)
    before = dict(row['review'])
    row['capture_classification'] = dict(capture_mode='rlt', source_manifest_sha256=row['metadata_sha256'])
    annotations.save_capture_classification(row)
    assert service.episode(row['id'], True)['review'] == before
    assert service.episode(row['id'], True)['source_signature'] == row['source_signature']
    saved = service.save_notes(row['id'], 'keep', 'operator', row['source_signature'], 'none')
    doc = json.loads((raw/annotations.NAME).read_text())
    assert doc['capture_classification']['capture_mode'] == 'rlt'
    assert service.episode(row['id'], True)['review']['notes'] == saved['notes']


def test_capture_tag_migration_preserves_review_exactly(tmp_path):
    raw, service, row = make_store(tmp_path)
    service.save_review(row['id'], 'approved', 'operator note', 'human', row['source_signature'], 'none')
    row = service.episode(row['id'], True)
    before = dict(row['review'])
    row['capture_classification'] = dict(capture_mode='demonstration', source_manifest_sha256=row['metadata_sha256'])
    annotations.save_capture_classification(row)
    assert service.episode(row['id'], True)['review'] == before
    content = (raw/annotations.NAME).read_bytes()
    annotations.save_capture_classification(row)
    assert (raw/annotations.NAME).read_bytes() == content
