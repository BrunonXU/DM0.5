"""Register the pinned geometry-cache overlay in native numerical/owner startup.

The production infra files stay immutable. An unavailable optimization leaves
the original constructor active; no cache result can authorize robot motion.
"""
from pathlib import Path
import hashlib
import importlib.util
import json
import platform
import sys
import time


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def register_mesh_cache(site, store=None, *, bundle=None):
    started = time.perf_counter()
    result = dict(enabled=False, motion_authorized=False)
    installed = []
    try:
        bundle = Path(bundle or Path(__file__).resolve().parents[2])
        manifest = json.loads((bundle / 'manifest.json').read_text())
        descriptor_path = bundle / 'configs/collision-cache-overlay.json'
        descriptor = json.loads(descriptor_path.read_text())
        if descriptor.get('schema') != 'f1-harness-collision-cache-overlay-v1':
            raise ValueError('unknown_cache_overlay')
        overlay = bundle / 'upstream/f1_safety'
        payloads = [descriptor_path, overlay / 'f1_mesh_asset_cache.py', overlay / 'f1_mesh_collision.py']
        for path in payloads:
            if (path.is_symlink() or path.resolve() != path
                    or _hash(path) != manifest['files'][str(path.relative_to(bundle))]):
                raise ValueError('cache_overlay_identity_changed')
        launch = json.loads((Path(site) / 'launch.local.json').read_text())
        runtime = Path(launch['runtime'])
        if not runtime.is_absolute():
            raise ValueError('absolute_cache_runtime_required')
        from f1dagger.config import pico_paths
        upper = pico_paths(Path(launch['infra_root']))
        base = upper / 'vendor/f1_safety/src/python/f1_mesh_collision.py'
        if base.is_symlink() or _hash(base) != descriptor['base_module_sha256']:
            raise ValueError('unsupported_base_mesh_evaluator')
        # Register once before any numerical/owner object is constructed. Never
        # replace a module which may already own native objects in this process.
        names = ('f1_mesh_asset_cache', 'f1_mesh_collision')
        if any(name in sys.modules for name in names):
            raise ValueError('mesh_evaluator_already_imported')
        import coal
        import mujoco
        binaries = sorted(Path(coal.__file__).parent.glob('*.so'))
        if not binaries:
            raise ValueError('coal_binary_identity_unavailable')
        abi = dict(machine=platform.machine(), byteorder=sys.byteorder,
                   python=sys.implementation.cache_tag, mujoco=mujoco.__version__,
                   coal=getattr(coal, '__version__', None),
                   binaries={p.name: _hash(p) for p in binaries},
                   sources={p.name: _hash(p) for p in payloads})
        for name in names:
            spec = importlib.util.spec_from_file_location(name, overlay / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            installed.append(name)
            spec.loader.exec_module(module)
        module.configure_asset_cache(runtime / 'static-geometry-cache', abi)
        result.update(enabled=True, abi_identity=hashlib.sha256(json.dumps(abi, sort_keys=True).encode()).hexdigest())
    except Exception as exc:
        # Remove only modules installed by this failed registration. Original
        # imports then follow the established pico_paths route unchanged.
        for name in installed:
            sys.modules.pop(name, None)
        result.update(reason=str(exc)[:200], error_type=type(exc).__name__)
    result['elapsed_ms'] = (time.perf_counter() - started) * 1000
    if store is not None:
        store.event('static_geometry_cache_registration', **result)
    return result
