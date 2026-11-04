import concurrent.futures
import hashlib
import json
from pathlib import Path

import pytest

from data_organization import DataOrganization, OrganizationConflict
from data_service import DataService
from test_data_auth import episode, http_app


@pytest.fixture
def store(tmp_path):
    episode(tmp_path/'raw')
    return DataService(tmp_path/'raw', tmp_path/'runtime', tmp_path/'web')


def command(store, **body):
    return store.organization.command(dict(expected_revision=store.organization.read()['revision'], **body), lambda: store.catalog(True))


def create(store, name='V1', parent='DAgger/人工示教'):
    return command(store, operation='create', name=name, parent=parent)['folders'][-1]['id']


def test_persistence_nested_moves_leave_raw_and_reviews_unchanged(store):
    row = store.catalog()['episodes'][0]
    raw = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in store.root.rglob('*') if p.is_file() and p.name!='f1-annotations.json'}
    store.save_review(row['id'], 'approved', 'accepted', 'test', row['source_signature'], row['review']['revision'])
    reviewed = store.episode(row['id'])
    parent = create(store)
    child = create(store, '补录', parent)
    moved = command(store, operation='move', target=child, episodes=[{'id': row['id'], 'source_signature': row['source_signature']}])
    command(store, operation='rename', folder_id=parent, name='V1_验收')
    command(store, operation='move', folder_id=parent, target='LXL')
    restarted = DataService(store.root, store.review_root.parent, store.web_root)
    after = restarted.episode(row['id'])
    assert (after['id'], after['path'], after['source_signature'], after['review']) == (reviewed['id'], reviewed['path'], reviewed['source_signature'], reviewed['review'])
    assert restarted.catalog()['organization']['memberships'] == moved['memberships']
    assert raw == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in store.root.rglob('*') if p.is_file() and p.name!='f1-annotations.json'}
    assert len(restarted.catalog()['organization']['folders']) == 2


@pytest.mark.parametrize('name', ['', ' ', '..', '.', 'a/b', 'a\\b', 'a\x00b', 'x'*81])
def test_invalid_names_are_not_written(store, name):
    with pytest.raises(ValueError):
        create(store, name)
    assert not store.organization.path.exists()


def test_cycle_duplicate_missing_destination_and_partial_batch_are_atomic(store):
    a = create(store)
    b = create(store, 'V2', a)
    before = store.organization.path.read_bytes()
    for body in [dict(operation='create', name='v1', parent='DAgger/人工示教'),
                 dict(operation='move', folder_id=a, target=a),
                 dict(operation='move', folder_id=a, target=b),
                 dict(operation='move', folder_id=a, target='@'+'0'*32)]:
        with pytest.raises(ValueError):
            command(store, **body)
        assert store.organization.path.read_bytes() == before
    row = store.catalog()['episodes'][0]
    with pytest.raises(OrganizationConflict, match='来源'):
        command(store, operation='move', target=b, episodes=[{'id': row['id'], 'source_signature': row['source_signature']}, {'id': '0'*24, 'source_signature': 'old'}])
    assert store.organization.path.read_bytes() == before
    (Path(row['path'])/'data_0.mcap').write_bytes(b'new source')
    with pytest.raises(OrganizationConflict):
        command(store, operation='move', target=b, episodes=[{'id': row['id'], 'source_signature': row['source_signature']}])
    assert store.organization.path.read_bytes() == before


def test_two_instances_competing_same_revision_one_wins(store):
    first, second = store.organization, DataOrganization(store.review_root.parent)
    revision = first.read()['revision']
    def save(pair):
        service, name = pair
        try:
            service.command(dict(operation='create', expected_revision=revision, name=name, parent=''), lambda: {'episodes': []})
            return 'saved'
        except OrganizationConflict:
            return 'conflict'
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(save, [(first, 'V1'), (second, 'V2')])) == ['conflict', 'saved']
    assert len(first.read()['folders']) == 1


def test_corrupt_metadata_does_not_hide_raw_or_overwrite_file(store):
    store.organization.path.parent.mkdir(parents=True, exist_ok=True)
    store.organization.path.write_text('{corrupt')
    catalog = store.catalog()
    assert len(catalog['episodes']) == 1 and catalog['organization_error']
    with pytest.raises(ValueError):
        command(store, operation='create', name='new', parent='')
    assert store.organization.path.read_text() == '{corrupt'


def test_http_requires_login_origin_revision_but_no_robot_lease(http_app):
    app, request = http_app
    status, listing, cookie = request('/api/data')
    body = dict(operation='create', parent='', name='V1', expected_revision=listing['organization']['revision'])
    assert request('/api/data/organization', body, cookie, origin=False)[0] == 403
    status, _, cookie = request('/api/login', {}, cookie)
    assert status == 200
    assert request('/api/data/organization', body, cookie, origin=False)[0] == 403
    status, result, _ = request('/api/data/organization', body, cookie)
    assert status == 200 and result['organization']['folders'][0]['name'] == 'V1'
    status, result, _ = request('/api/data/organization', body, cookie)
    assert status == 409 and result['code'] == 'organization_conflict'
    assert app.lease.fd is None and app.actions.latest() is None
