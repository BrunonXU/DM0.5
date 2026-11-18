"""Snapshot copies and identity checks; never a live controller acceptance."""
import hashlib,json,os,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from audit_assets import URDF,prepare_assets,load_assets


@pytest.fixture
def source(tmp_path):
    root=tmp_path/'source';root.mkdir()
    data={URDF:b'<robot name="test"/>','configs/model_mapping.json':b'{}','robot/motion_plan.py':b'# fixed\n'}
    for n,b in data.items():
        p=root/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
    binding=tmp_path/'binding.json';binding.write_text(json.dumps(dict(schema='f1_dagger_dependency_binding_v1',files={n:hashlib.sha256(b).hexdigest() for n,b in data.items()})))
    yield root,binding,tmp_path
    for p in sorted(tmp_path.rglob('*'),key=lambda p:len(p.parts)):
        if not p.is_symlink():p.chmod(0o755 if p.is_dir() else 0o644)


def test_copy_is_independent_and_binds_spec(source):
    root,binding,tmp=source;dest=tmp/'assets';m=prepare_assets(root,binding,dest);assets=load_assets(dest,expected_id=m['asset_id'])
    original=(dest/URDF).read_bytes();(root/URDF).write_bytes(b'changed source')
    assert assets.unchanged() and (dest/URDF).read_bytes()==original
    assert (root/URDF).stat().st_ino!=(dest/URDF).stat().st_ino
    spec=dict(urdf=str(root/URDF),gate=dict(config={'_infra_root':str(root),'_infra_manifest_sha256':m['base_binding_sha256']}))
    changed=assets.relocate(spec);assets.verify_spec(changed)
    assert changed['urdf']==str(dest/URDF) and spec['urdf']==str(root/URDF)
    assert not m['production_approved'] and not m['motion_authorized']


def test_drift_is_explicit_not_release_approval(source):
    root,binding,tmp=source;(root/'robot/motion_plan.py').write_text('changed')
    with pytest.raises(ValueError,match='differs_from_bound'):
        prepare_assets(root,binding,tmp/'strict')
    assert not (tmp/'strict/assets.json').exists()
    m=prepare_assets(root,binding,tmp/'development',development_drift=True)
    assert set(m['base_differences'])=={'robot/motion_plan.py'}
    assert not m['production_approved']


def test_changed_content_even_same_mtime_invalidates_warm_snapshot(source):
    root,binding,tmp=source;dest=tmp/'assets';prepare_assets(root,binding,dest);a=load_assets(dest)
    p=dest/'configs/model_mapping.json';old=p.stat();p.chmod(0o644);p.write_bytes(b'[]');os.utime(p,ns=(old.st_atime_ns,old.st_mtime_ns));p.chmod(0o444)
    assert not a.unchanged()
    with pytest.raises(ValueError,match='content_mismatch'):load_assets(dest)


def test_replacement_and_added_file_invalidate_identity(source):
    root,binding,tmp=source;dest=tmp/'assets';prepare_assets(root,binding,dest);a=load_assets(dest)
    directory=dest/'robot';directory.chmod(0o755);(directory/'extra.py').write_text('extra');directory.chmod(0o555)
    assert not a.unchanged()
    with pytest.raises(ValueError,match='undeclared'):load_assets(dest)


@pytest.mark.parametrize('kind',['wrong_id','writable','symlink'])
def test_wrong_identity_or_mutable_or_linked_snapshot_refused(source,kind):
    root,binding,tmp=source;dest=tmp/'assets';prepare_assets(root,binding,dest)
    if kind=='wrong_id':
        with pytest.raises(ValueError,match='identity'):load_assets(dest,expected_id='x'*64)
    elif kind=='writable':
        (dest/URDF).chmod(0o644)
        with pytest.raises(ValueError,match='read_only'):load_assets(dest)
    else:
        link=tmp/'link';link.symlink_to(dest,target_is_directory=True)
        with pytest.raises(ValueError,match='symlink'):load_assets(link)


def test_source_symlink_and_escape_are_not_copied(source):
    root,binding,tmp=source;p=root/'robot/motion_plan.py';p.unlink();p.symlink_to(binding)
    with pytest.raises(ValueError,match='symlink'):prepare_assets(root,binding,tmp/'linked')
    value=json.loads(binding.read_text());value['files']['../outside']='0'*64;binding.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='invalid_numerical_asset_path'):prepare_assets(root,binding,tmp/'escape')


def test_wrong_model_or_base_binding_not_relocated(source):
    root,binding,tmp=source;dest=tmp/'assets';m=prepare_assets(root,binding,dest);a=load_assets(dest)
    spec=dict(urdf=str(root/URDF),gate=dict(config={'_infra_root':str(root),'_infra_manifest_sha256':'wrong'}))
    with pytest.raises(ValueError,match='base_manifest'):a.relocate(spec)
    spec['gate']['config']['_infra_manifest_sha256']=m['base_binding_sha256'];spec['urdf']=str(root/'other.urdf')
    with pytest.raises(ValueError,match='canonical'):a.relocate(spec)


def test_extra_file_needs_exact_declared_content(source):
    root,binding,tmp=source;p=root/'extra.py';p.write_text('# extension')
    with pytest.raises(ValueError,match='differs_from_bound'):
        prepare_assets(root,binding,tmp/'bad-extra',extras={'extra.py':'0'*64},development_drift=True)
    expected=hashlib.sha256(p.read_bytes()).hexdigest()
    m=prepare_assets(root,binding,tmp/'good-extra',extras={'extra.py':expected})
    assert m['extra_files']=={'extra.py':expected}


def test_change_notifications_avoid_rehashing_unchanged_files(source,monkeypatch):
    import audit_assets as mod
    root,binding,tmp=source;dest=tmp/'assets';prepare_assets(root,binding,dest);a=load_assets(dest)
    assert a.validation_mode=='content_hash_then_inotify'
    def no_rehash(*args):raise AssertionError('unchanged hot path rehashed files')
    monkeypatch.setattr(mod,'file_hash',no_rehash)
    for _ in range(5):assert a.unchanged()
    p=dest/URDF;p.chmod(0o644);p.write_text('changed');p.chmod(0o444)
    assert not a.unchanged()  # Kernel invalidation, without relying on stat.
    a.close()


def test_unavailable_notification_falls_back_to_full_content_checks(source,monkeypatch):
    import audit_asset_watch,audit_assets
    class Unavailable:
        fd=None
        def __init__(self,*args):pass
        def changed(self):return False
        def close(self):pass
    root,binding,tmp=source;dest=tmp/'assets';prepare_assets(root,binding,dest)
    monkeypatch.setattr(audit_asset_watch,'AssetWatch',Unavailable)
    a=load_assets(dest);assert a.validation_mode=='content_hash_per_check'
    calls=[];original=audit_assets.file_hash
    def count(p):calls.append(p);return original(p)
    monkeypatch.setattr(audit_assets,'file_hash',count)
    assert a.unchanged() and len(calls)==len(a.manifest['files'])
    p=dest/'configs/model_mapping.json';old=p.stat();p.chmod(0o644);p.write_bytes(b'[]');os.utime(p,ns=(old.st_atime_ns,old.st_mtime_ns));p.chmod(0o444)
    assert not a.unchanged()
