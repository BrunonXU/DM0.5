"""Content-bound, read-only offline numerical dependency snapshots.

Reuse the F1 dependency manifest; development drift is explicit evidence, never
production approval. Copies are independent of editable source files. Read-only
modes are an operational convention, not a security boundary against the owner.
"""
from copy import deepcopy
from pathlib import Path,PurePosixPath
import json,os,stat

from f1_harness.contracts import digest
from f1_harness.controller_reuse import file_hash

SCHEMA='f1-offline-numerical-assets-v1'
URDF='web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'


def _relative(name):
    if not isinstance(name,str):raise ValueError('invalid_numerical_asset_path')
    p=PurePosixPath(name)
    if not isinstance(name,str) or not name or p.is_absolute() or '..' in p.parts or str(p)!=name:
        raise ValueError('invalid_numerical_asset_path')
    return p


def _regular(root,name):
    _relative(name);p=root/name
    for q in (p,*p.parents):
        if q.is_symlink():raise ValueError('symlink_numerical_asset')
        if q==root:break
    if not stat.S_ISREG(p.stat().st_mode):raise ValueError('nonregular_numerical_asset')
    return p


def _stamp(path):
    s=path.lstat()
    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode)


def prepare_assets(source,binding,destination,*,extras=None,development_drift=False):
    source=Path(source).resolve(strict=True);binding=Path(binding)
    raw=binding.read_bytes();base=json.loads(raw)
    if base.get('schema')!='f1_dagger_dependency_binding_v1' or not base.get('files'):
        raise ValueError('existing_F1_dependency_manifest_required')
    files=dict(base['files']);extras=dict(extras or {})
    if set(files)&set(extras):raise ValueError('asset_overlay_must_not_replace_base_identity')
    files.update(extras)
    if URDF not in files or 'configs/model_mapping.json' not in files:
        raise ValueError('numerical_model_binding_missing')
    destination=Path(destination).absolute();destination.mkdir(parents=True,exist_ok=False)
    copied={};drift={}
    # An interrupted preparation remains an incomplete directory without a
    # manifest. Never repair/reuse it as though it were a published snapshot.
    for name,expected in sorted(files.items()):
        p=_regular(source,name);before=_stamp(p);data=p.read_bytes()
        import hashlib
        actual=hashlib.sha256(data).hexdigest()
        if _stamp(p)!=before:raise ValueError('asset_source_changed_during_copy')
        if actual!=expected:
            if name in extras or not development_drift:raise ValueError('asset_differs_from_bound_source:'+name)
            drift[name]=dict(expected=expected,actual=actual)
        target=destination/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(data);target.chmod(0o444);copied[name]=actual
    import hashlib
    payload=dict(schema=SCHEMA,base_binding_sha256=hashlib.sha256(raw).hexdigest(),
        files=copied,base_differences=drift,extra_files=extras,
        production_approved=False,motion_authorized=False)
    manifest=dict(**payload,asset_id=digest(payload))
    p=destination/'assets.json';p.write_text(json.dumps(manifest,indent=2)+'\n');p.chmod(0o444)
    for directory in sorted((p for p in destination.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):
        directory.chmod(0o555)
    destination.chmod(0o555)
    load_assets(destination,expected_id=manifest['asset_id'])
    return manifest


class AssetSnapshot:
    def __init__(self,root,manifest,stamps):
        self.root=root;self.manifest=manifest;self.asset_id=manifest['asset_id'];self.stamps=stamps
        from audit_asset_watch import AssetWatch
        self.watch=AssetWatch(stamps)
        self.verified=False

    @property
    def validation_mode(self):
        return 'content_hash_then_inotify' if self.watch.fd is not None else 'content_hash_per_check'

    def close(self):
        self.watch.close();self.verified=False

    def unchanged(self):
        try:
            if self.watch.changed():return False
            if self.verified and self.watch.fd is not None:return True
            if not all(_stamp(p)==stamp for p,stamp in self.stamps.items()):return False
            # Metadata alone is insufficient: filesystems may preserve coarse
            # timestamps across a same-size edit. Never reuse a cached verdict
            # solely because stat metadata is identical.
            for name,expected in self.manifest['files'].items():
                p=self.root/name
                if file_hash(p)!=expected or _stamp(p)!=self.stamps[p]:return False
            if json.loads((self.root/'assets.json').read_text())!=self.manifest:return False
            actual={str(p.relative_to(self.root)) for p in self.root.rglob('*') if not p.is_dir()}
            if actual!=set(self.manifest['files'])|{'assets.json'}:return False
            if not all(_stamp(p)==stamp for p,stamp in self.stamps.items()) or self.watch.changed():return False
            self.verified=True
            return True
        except (OSError,ValueError):return False

    def relocate(self,spec):
        value=deepcopy(spec)
        old=Path(value['gate']['config']['_infra_root']).resolve()
        if Path(value['urdf']).resolve()!=old/URDF:
            raise ValueError('numerical_urdf_not_canonical_bound_model')
        if value['gate']['config']['_infra_manifest_sha256']!=self.manifest['base_binding_sha256']:
            raise ValueError('numerical_base_manifest_identity_mismatch')
        value['urdf']=str(self.root/URDF)
        value['gate']['config']['_infra_root']=str(self.root)
        return value

    def verify_spec(self,spec):
        if (spec['urdf']!=str(self.root/URDF)
                or spec['gate']['config']['_infra_root']!=str(self.root)
                or spec['gate']['config']['_infra_manifest_sha256']!=self.manifest['base_binding_sha256']):
            raise ValueError('numerical_asset_spec_mismatch')


def load_assets(root,*,expected_id=None):
    root=Path(root).absolute()
    if root.is_symlink():raise ValueError('symlink_asset_root')
    p=_regular(root,'assets.json');before=_stamp(p);m=json.loads(p.read_text())
    payload={k:v for k,v in m.items() if k!='asset_id'}
    if (m.get('schema')!=SCHEMA or not m.get('files') or digest(payload)!=m.get('asset_id')
            or (expected_id is not None and m['asset_id']!=expected_id)):
        raise ValueError('numerical_asset_manifest_identity_mismatch')
    if URDF not in m['files'] or 'configs/model_mapping.json' not in m['files']:
        raise ValueError('numerical_model_binding_missing')
    stamps={p:before};directories={root}
    for name,expected in m['files'].items():
        file=_regular(root,name);stamp=_stamp(file)
        if file_hash(file)!=expected or _stamp(file)!=stamp:
            raise ValueError('numerical_asset_content_mismatch:'+name)
        stamps[file]=stamp
        for q in file.parents:
            directories.add(q)
            if q==root:break
    for directory in directories:stamps[directory]=_stamp(directory)
    if any(stamp[-1]&0o222 for stamp in stamps.values()):
        raise ValueError('numerical_asset_snapshot_must_be_read_only')
    # No undeclared importable files, meshes or configuration beside the list.
    actual={str(p.relative_to(root)) for p in root.rglob('*') if not p.is_dir()}
    if actual!=set(m['files'])|{'assets.json'}:raise ValueError('undeclared_numerical_asset')
    result=AssetSnapshot(root,m,stamps)
    if not result.unchanged():raise ValueError('numerical_assets_changed_during_validation')
    return result
