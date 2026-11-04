"""Notes are optional annotations, not new approvals or robot actions."""
import concurrent.futures
import json
import pytest
from data_service import DataService, ReviewConflict
from test_data_auth import episode, http_app


def store_and_row(tmp_path):
    raw=episode(tmp_path/'raw')
    store=DataService(tmp_path/'raw',tmp_path/'runtime',tmp_path/'web')
    return store,store.catalog()['episodes'][0],raw


def test_notes_preserve_review_identity_and_raw_files(tmp_path):
    store,row,raw=store_and_row(tmp_path)
    before={p.name:p.read_bytes() for p in raw.iterdir() if p.name!='f1-annotations.json'}
    approved=store.save_review(row['id'],'approved','old note','reviewer',row['source_signature'],'none')
    saved=store.save_notes(row['id'],'<script>literal text</script>\n[8.1s] hold','editor',row['source_signature'],approved['revision'])
    for key in ('status','reviewer','updated_at','scope','source_signature'):
        assert saved[key]==approved[key]
    assert saved['notes_editor']=='editor' and saved['notes_updated_at']
    assert saved['notes']=='<script>literal text</script>\n[8.1s] hold'
    assert before=={p.name:p.read_bytes() for p in raw.iterdir() if p.name!='f1-annotations.json'}
    assert saved['revision']!=approved['revision']
    assert 'revision' not in json.loads((raw/'f1-annotations.json').read_text())['review']
    # A review loaded before the note edit cannot overwrite the new note.
    with pytest.raises(ReviewConflict):
        store.save_review(row['id'],'rejected','old','reviewer',row['source_signature'],approved['revision'])


def test_first_note_and_clear_never_approve(tmp_path):
    store,row,_=store_and_row(tmp_path)
    saved=store.save_notes(row['id'],'layout A','editor',row['source_signature'],'none')
    assert saved['status']=='pending' and saved['updated_at'] is None and not saved.get('reviewer')
    cleared=store.save_notes(row['id'],'','editor',row['source_signature'],saved['revision'])
    assert cleared['notes']=='' and cleared['status']=='pending'


def test_notes_competing_writes_and_source_change(tmp_path):
    store,row,raw=store_and_row(tmp_path)
    def save(text):
        try:return store.save_notes(row['id'],text,'editor',row['source_signature'],'none')
        except ReviewConflict as error:return error
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results=list(pool.map(save,['one','two']))
    assert sum(isinstance(r,dict) for r in results)==1
    assert next(r for r in results if isinstance(r,ReviewConflict)).code=='REVIEW_CONFLICT'
    prior=store.episode(row['id'],True)['review']
    (raw/'data_0.mcap').write_bytes(b'changed')
    with pytest.raises(ReviewConflict,match='来源'):
        store.save_notes(row['id'],'wrong source','editor',row['source_signature'],prior['revision'])
    current=store.episode(row['id'],True)
    assert current['review']['stale'] and current['review']['notes']==''
    saved=store.save_notes(row['id'],'new source note','editor',current['source_signature'],current['review']['revision'])
    assert saved['status']=='pending' and saved['previous_review']['notes']==prior['notes']


@pytest.mark.parametrize('notes',[None,{},'x'*4001])
def test_notes_validate_text_before_writing(tmp_path,notes):
    store,row,_=store_and_row(tmp_path)
    with pytest.raises(ValueError):store.save_notes(row['id'],notes,'editor',row['source_signature'],'none')
    assert not store.review_root.exists()


def test_notes_require_revision_and_allow_max_length(tmp_path):
    store,row,_=store_and_row(tmp_path)
    with pytest.raises(ReviewConflict) as result:store.save_notes(row['id'],'text','editor',row['source_signature'])
    assert result.value.code=='REVIEW_REFRESH_REQUIRED'
    assert store.save_notes(row['id'],'字'*4000,'editor',row['source_signature'],'none')['notes']=='字'*4000


def test_http_notes_origin_and_no_control_lease(http_app):
    app,request=http_app
    _,listing,cookie=request('/api/data');row=listing['episodes'][0]
    body=dict(episode_id=row['id'],notes='layout B',expected_source_signature=row['source_signature'],expected_review_revision='none',status='approved')
    assert request('/api/data/notes',body,cookie,origin=False)[0]==403
    status,data,_=request('/api/data/notes',body,cookie)
    assert status==200 and data['review']['status']=='pending'
    assert data['review']['notes']=='layout B'
    status,data,_=request('/api/data/notes',body,cookie)
    assert status==409 and data['code']=='REVIEW_CONFLICT'
    assert app.lease.fd is None and app.actions.latest() is None
