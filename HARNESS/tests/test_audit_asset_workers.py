"""Snapshot identity through the local worker lifecycle, no numerical devices."""
from pathlib import Path
import sys,pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from audit_assets import prepare_assets,URDF
from audit_workers import AuditPool,AuditWorkerFailed
from test_audit_assets import source
from test_audit_workers import job,assert_reaped


def make(source):
    root,binding,tmp=source;dest=tmp/'assets';m=prepare_assets(root,binding,dest)
    spec=dict(urdf=str(root/URDF),gate=dict(config={'_infra_root':str(root),'_infra_manifest_sha256':m['base_binding_sha256']}))
    pool=AuditPool(spec,tmp/'workers',assets=dest,_worker_command=[sys.executable,str(Path(__file__).parent/'fixtures/audit_worker_fake.py')])
    return pool,root,dest,m


def test_snapshot_is_echoed_and_source_edit_does_not_affect_warm_workers(source):
    p,root,dest,m=make(source);p.start()
    try:
        (root/URDF).write_text('source changed after warmup')
        assert p.asset_id==m['asset_id'] and p.map([job()],context_binding='a'*64)[0]['samples']==1
        assert p.spec['urdf']==str(dest/URDF)
    finally:p.close()


def test_mutated_snapshot_discards_pool_before_next_job(source):
    p,root,dest,m=make(source);p.start();children=list(p.children)
    file=dest/URDF;file.chmod(0o644);file.write_text('tampered');file.chmod(0o444)
    with pytest.raises(AuditWorkerFailed,match='assets_changed'):
        p.map([job()],context_binding='a'*64)
    assert_reaped(p,children)


def test_mutated_snapshot_is_not_reloaded_under_old_identity(source):
    p,root,dest,m=make(source);file=dest/URDF;file.chmod(0o644);file.write_text('tampered');file.chmod(0o444)
    with pytest.raises(AuditWorkerFailed,match='assets_changed'):p.start()
    assert not p.children
