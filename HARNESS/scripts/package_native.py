"""Build a credential-free native development bundle; never activates a robot release."""
from pathlib import Path
import argparse
import hashlib
import json
import tarfile
import importlib.util
from io import BytesIO


def build_bundle(output, wheels, *, root=None):
    root=Path(root or Path(__file__).resolve().parents[1])
    output,wheels=Path(output),Path(wheels)
    if output.exists():raise FileExistsError('new output directory required')
    files={}
    for name in ('src','native'):
        for path in (root/name).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix in ('.py','.md','.lock'):
                files[str(path.relative_to(root))]=path
    upstream=root/'third_party/RPent'
    for path in (upstream/'rpent').rglob('*.py'):
        files['upstream/RPent/'+str(path.relative_to(upstream))]=path
    files['upstream/RPent/LICENSE']=upstream/'LICENSE'
    for path in (root/'configs').glob('*'):
        if path.name.endswith(('.example.json','.example.toml')) or path.name in ('observation.readonly.json','upstreams.lock.json','collision-cache-overlay.json','fastdds-udp-only.xml'):
            files['configs/'+path.name]=path
    # A registered, manifest-bound overlay from the canonical shared component;
    # never edit the frozen production infra dependency in place.
    mesh_source=root.parent/'F1-4242/integrations/pico_upper/vendor/f1_safety/src/python'
    for name in ('f1_mesh_collision.py','f1_mesh_asset_cache.py'):
        files['upstream/f1_safety/'+name]=mesh_source/name
    for path in wheels.glob('*.whl'):files['wheels/'+path.name]=path
    for name in ('ros_capture.py','camera_timing_probe.py','install_native_bundle.py','serve_workspace.py'):
        path=root/'scripts'/name
        if path.is_file():files['scripts/'+name]=path
    if not any(name.startswith('wheels/') for name in files):raise ValueError('native_wheels_required')
    # Reuse the established F1 source collector. Only the native owner and its pinned
    # core/assets/configuration enter this bundle; no Windows tools or robot data.
    packager=root.parent/'DAgger/F1/scripts/53_package_f1.py'
    spec=importlib.util.spec_from_file_location('_f1_source_packager',packager)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    control={name:path for name,path in module.source_files().items()
             if name.startswith(('src/f1dagger/','src/dagger_core/','configs/'))}
    files.update({'control/'+name:path for name,path in control.items()})
    for name,path in files.items():
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError('symlink_source_refused:'+name)
    payloads={name:path.read_bytes() for name,path in files.items()}
    control_manifest=dict(schema='f1_dagger_release_v1',robot='F1-09',deployment_verified=False,
                          layout='platform_shared_v2',product_version=None,
                          files={name:hashlib.sha256(payloads['control/'+name]).hexdigest() for name in control})
    payloads['control/F1_RELEASE_MANIFEST.json']=(json.dumps(control_manifest,indent=2)+'\n').encode()
    hashes={name:hashlib.sha256(data).hexdigest() for name,data in payloads.items()}
    identity=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
    manifest=dict(schema='f1-native-development-bundle-v1',bundle_id=identity,product_version=None,
                  production_activated=False,control_source='bundled',
                  control_manifest_sha256=hashes['control/F1_RELEASE_MANIFEST.json'],files=hashes)
    output.mkdir(parents=True)
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    payloads['manifest.json']=(output/'manifest.json').read_bytes()
    with tarfile.open(output/'bundle.tar.gz','w:gz') as tar:
        for name,data in sorted(payloads.items()):
            info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o644
            tar.addfile(info,BytesIO(data))
    return dict(bundle_id=identity,files=len(hashes),archive=str(output/'bundle.tar.gz'),
                control_manifest_sha256=manifest['control_manifest_sha256'],production_activated=False)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--wheels',type=Path,required=True);args=p.parse_args()
    print(json.dumps(build_bundle(args.output,args.wheels)))


if __name__=='__main__':main()
