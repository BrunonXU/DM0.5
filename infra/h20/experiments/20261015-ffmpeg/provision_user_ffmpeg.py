#!/usr/bin/env python3
"""H20: download verified Ubuntu debs and extract to a user prefix, never apt commit."""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PREFIX = Path('/u01/user/tools/ffmpeg-4.4.2-jammy')
STAGING = Path('/u01/user/staging/ffmpeg-4.4.2-jammy')
PROXY = 'http://192.0.2.34:3128'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def download(item):
    target = STAGING / 'debs' / Path(item['filename']).name
    if target.exists() and digest(target) == item['sha256']:
        return dict(item, downloaded_from='verified existing staging file', verified=True)
    errors = []
    for base, proxy in [('https://archive.ubuntu.com/ubuntu/', PROXY),
                        ('https://security.ubuntu.com/ubuntu/', PROXY),
                        ('https://company.example.invalid/ubuntu/', None)]:
        partial = target.with_suffix('.partial')
        url = base + item['filename']
        cmd = ['curl', '--fail', '--silent', '--show-error', '--location',
               '--proto', '=https', '--proto-redir', '=https', '--connect-timeout', '5',
               '--max-time', '60', '--output', str(partial)]
        cmd += ['--proxy', proxy, '--noproxy', ''] if proxy else ['--noproxy', '*']
        result = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=65)
        if result.returncode == 0 and partial.stat().st_size == item['size'] and digest(partial) == item['sha256']:
            partial.replace(target)
            print('verified ' + item['name'], file=sys.stderr, flush=True)
            return dict(item, downloaded_from=url, verified=True)
        errors.append({'url': url, 'returncode': result.returncode, 'error': result.stderr[-600:]})
        partial.unlink(missing_ok=True)
    return dict(item, verified=False, errors=errors)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--replacement-manifest')
    args = parser.parse_args()
    assert os.getuid() == 1004 and Path('/u01').is_mount(), 'Unexpected account or unmounted data disk'
    import apt
    import apt_pkg
    apt_pkg.config.set('APT::Install-Recommends', '0')
    cache = apt.Cache()
    assert cache['ffmpeg'].candidate.version == '7:4.4.2-0ubuntu0.22.04.1'
    cache['ffmpeg'].mark_install(auto_fix=True, from_user=True)  # Memory-only solver.
    packages = []
    for package in cache.get_changes():
        version = package.candidate
        assert not package.installed, 'Refuse private copies of upgrades to installed system packages'
        assert version.record.get('SHA256') and version.filename.startswith('pool/')
        assert package.name not in ('libc6', 'libstdc++6', 'libgcc-s1')
        packages.append({'name': package.name, 'version': version.version, 'size': version.size,
                         'filename': version.filename, 'sha256': version.record.get('SHA256')})
    packages.sort(key=lambda p: p['name'])
    if args.replacement_manifest:
        replacement_path = Path(args.replacement_manifest)
        assert replacement_path.resolve().is_relative_to(STAGING.resolve())
        replacements = json.loads(replacement_path.read_text())
        by_name = {p['name']: p for p in replacements['packages']}
        assert set(by_name) <= {'libgfortran5', 'libopenjp2-7', 'libssh-gcrypt-4', 'libvpx7'}
        packages = [by_name.get(p['name'], p) for p in packages]
    plan = {'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'prefix': str(PREFIX), 'metadata_source': 'H20 existing Ubuntu Jammy apt cache',
            'system_modified': False, 'packages': packages,
            'total_download_bytes': sum(p['size'] for p in packages)}
    if args.replacement_manifest:
        plan['updated_metadata'] = replacements
    if not args.apply:
        print(json.dumps(plan, indent=2))
        return
    if PREFIX.exists():
        raise SystemExit('Prefix already exists; inspect rather than overwrite')
    (STAGING / 'debs').mkdir(parents=True, exist_ok=True, mode=0o700)
    (STAGING / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(download, packages))
    plan['packages'] = results
    (STAGING / 'download-manifest.json').write_text(json.dumps(plan, indent=2) + '\n')
    failed = [p['name'] for p in results if not p['verified']]
    if failed:
        print(json.dumps({'status': 'download_incomplete', 'failed': failed, 'manifest': str(STAGING / 'download-manifest.json')}, indent=2))
        raise SystemExit(1)
    PREFIX.parent.mkdir(parents=True, exist_ok=True)
    building = Path(tempfile.mkdtemp(prefix='.ffmpeg-building-', dir=PREFIX.parent))
    for item in results:
        subprocess.run(['dpkg-deb', '--extract', str(STAGING / 'debs' / Path(item['filename']).name), str(building)], check=True)
    # The Ubuntu packages contain their canonical relative SONAME symlinks.
    # BLAS/LAPACK and PulseAudio use subdirectories; include only actual library dirs.
    relative_libs = ['usr/lib/x86_64-linux-gnu', 'usr/lib/x86_64-linux-gnu/blas',
                     'usr/lib/x86_64-linux-gnu/lapack', 'usr/lib/x86_64-linux-gnu/pulseaudio']
    env = os.environ.copy()
    env['LD_LIBRARY_PATH'] = ':'.join(str(building / p) for p in relative_libs if (building / p).is_dir())
    checked = {}
    for relative in ['usr/bin/ffmpeg', 'usr/bin/ffprobe', 'usr/lib/x86_64-linux-gnu/libavcodec.so.58',
                     'usr/lib/x86_64-linux-gnu/libavformat.so.58', 'usr/lib/x86_64-linux-gnu/libavutil.so.56']:
        result = subprocess.run(['ldd', str(building / relative)], env=env, capture_output=True, text=True)
        checked[relative] = result.stdout + result.stderr
        if result.returncode or 'not found' in checked[relative]:
            (STAGING / 'ldd-failure.json').write_text(json.dumps(checked, indent=2) + '\n')
            raise SystemExit('Unresolved runtime libraries; incomplete prefix retained at ' + str(building))
    (building / 'manifest.json').write_text(json.dumps(plan, indent=2) + '\n')
    building.rename(PREFIX)
    PREFIX.chmod(0o750)
    library_path = ':'.join(str(PREFIX / p) for p in relative_libs if (PREFIX / p).is_dir())
    # Explicitly sourced by a job, never inserted into shell rc or global linker config.
    activation = ('# Source only inside the intended job shell.\n'
                  'export PATH="' + str(PREFIX / 'usr/bin') + ':$PATH"\n'
                  'export LD_LIBRARY_PATH="' + library_path + '${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n')
    (PREFIX / 'activate.sh').write_text(activation)
    (PREFIX / 'activate.sh').chmod(0o640)
    env['LD_LIBRARY_PATH'] = library_path
    version = subprocess.run([str(PREFIX / 'usr/bin/ffmpeg'), '-version'], env=env, capture_output=True, text=True, check=True)
    receipt = {'status': 'installed_user_prefix', 'prefix': str(PREFIX), 'packages': len(results),
               'total_download_bytes': plan['total_download_bytes'], 'ldd': checked,
               'ffmpeg_version': version.stdout, 'activation': str(PREFIX / 'activate.sh')}
    (STAGING / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
