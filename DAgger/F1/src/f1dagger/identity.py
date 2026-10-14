"""Bind the loaded F1 code, immutable release, and accepted configuration."""
import hashlib
import json
from pathlib import Path
from .config import DAGGER_ROOT
from .contract import sha256


def runtime_identity(config, *, root=DAGGER_ROOT, powered=False):
    root = Path(root).resolve()
    path = root/'F1_RELEASE_MANIFEST.json'
    source_roots = {'src/f1dagger':root/'src/f1dagger', 'src/dagger_core':root/'src/dagger_core'}
    if not source_roots['src/dagger_core'].is_dir() and not path.exists():
        source_roots['src/dagger_core'] = root.parent/'shared/src/dagger_core'
    files = {prefix+'/'+p.relative_to(source).as_posix():sha256(p)
             for prefix,source in source_roots.items() for p in sorted(source.rglob('*.py'))}
    digest = hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    manifest_hash = None
    if path.exists():
        manifest_hash = sha256(path)
        manifest = json.loads(path.read_text())
        for name, expected in manifest['files'].items():
            resolved = (root/name).resolve()
            if root not in resolved.parents or sha256(resolved) != expected:
                raise ValueError('F1 release file mismatch: '+name)
        if any(manifest['files'].get(name) != actual for name,actual in files.items()):
            raise ValueError('F1 source is not covered by release manifest')
    expected = config.get('accepted_release_manifest_sha256')
    if expected is not None and expected != manifest_hash:
        raise ValueError('accepted F1 configuration belongs to a different release')
    if powered and (manifest_hash is None or expected is None):
        raise ValueError('powered F1 requires a manifest-bound accepted configuration')
    return dict(release=root.parent.name if manifest_hash else 'development',
        release_manifest_sha256=manifest_hash, source_sha256=digest,
        config_sha256=config['_config_sha256'], source_root=str(root),
        limit_controller='continuous_feasible_v1')


def ack_identity(identity):
    return ';release='+identity['release']+';source='+identity['source_sha256'][:12]+\
        ';config='+identity['config_sha256'][:12]
