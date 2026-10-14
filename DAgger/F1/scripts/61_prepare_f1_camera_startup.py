#!/usr/bin/env python3
"""Generate a reviewable F1-09 startup copy; never execute, deploy, or edit a profile.

The source must be an explicitly selected copy of the existing robot start_all.sh.
--source-sha256 binds that source; --wrapper and --robot-output-dir are absolute
robot release paths under /home/user/Bruno, not paths to execute on this host.
The generated profile_patch.json is a proposal for the next normal onsite base
startup. It is never merged into a profile by this program.
"""
import argparse
import difflib
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import tempfile


ORIGINAL_DIRECTORY_LINE = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
ORIGINAL_LOG_DIRECTORY_LINE = 'LOG_DIR="$SCRIPT_DIR/logs2"'
CAMERA_LINES = {
    'head': ('ros2 launch orbbec_camera head_gemini330.launch.py', 'head_camera.log'),
    'right': ('ros2 launch realsense2_camera rs_launch_right_wrist_d405.py', 'right_wrist.log'),
    'left': ('ros2 launch realsense2_camera rs_launch_left_wrist_d405.py', 'left_wrist.log'),
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def robot_path(value, *, bruno=False):
    path = PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts or '\n' in value or '\r' in value:
        raise ValueError('Expected an absolute robot path without traversal or newlines: ' + value)
    if bruno and PurePosixPath('/home/user/Bruno') not in path.parents:
        raise ValueError('New robot artifacts must be inside /home/user/Bruno: ' + value)
    return path


def replace_unique_line(text, before, after):
    lines = text.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.rstrip('\r\n') == before]
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one unchanged source line, found {len(matches)}: {before}')
    index = matches[0]
    ending = lines[index][len(before):]
    lines[index] = after + ending
    return ''.join(lines)


def render_startup(source, source_robot_path, wrapper, robot_log_dir):
    """Only working/log directories and three exact camera commands may change."""
    original = source.decode('utf-8')
    text = replace_unique_line(original, ORIGINAL_DIRECTORY_LINE,
                               'SCRIPT_DIR=' + shlex.quote(str(source_robot_path.parent)))
    text = replace_unique_line(text, ORIGINAL_LOG_DIRECTORY_LINE,
                               'LOG_DIR=' + shlex.quote(str(robot_log_dir)))
    replacements = []
    for side, (command, logfile) in CAMERA_LINES.items():
        suffix = f' > "$LOG_DIR/{logfile}" 2>&1 &'
        before = command + suffix
        after = 'bash ' + shlex.quote(str(wrapper)) + ' start ' + side + suffix
        text = replace_unique_line(text, before, after)
        replacements.append({'camera': side, 'before': before, 'after': after})
    # Exact reversibility verifies that all other source bytes remain intact.
    reverted = text
    for row in reversed(replacements):
        reverted = replace_unique_line(reverted, row['after'], row['before'])
    reverted = replace_unique_line(reverted, 'LOG_DIR=' + shlex.quote(str(robot_log_dir)),
                                  ORIGINAL_LOG_DIRECTORY_LINE)
    reverted = replace_unique_line(reverted,
                                  'SCRIPT_DIR=' + shlex.quote(str(source_robot_path.parent)),
                                  ORIGINAL_DIRECTORY_LINE)
    if reverted.encode('utf-8') != source:
        raise ValueError('Unexpected modification outside the five approved source lines')
    return text.encode('utf-8'), replacements


def prepare(*, source, source_sha256, source_robot_path, wrapper, output_dir, robot_output_dir,
            robot_log_dir='/home/user/Bruno/DAgger-runtime/base-start-logs'):
    source = Path(source).resolve(strict=True)
    output_dir = Path(output_dir).absolute()
    if output_dir.exists():
        raise FileExistsError('Refusing to overwrite existing review output: ' + str(output_dir))
    source_robot_path = robot_path(source_robot_path)
    wrapper = robot_path(wrapper, bruno=True)
    robot_log_dir = robot_path(robot_log_dir, bruno=True)
    destination = robot_path(robot_output_dir, bruno=True) / 'start_all.sh'
    if source_robot_path == destination or wrapper == destination:
        raise ValueError('Generated startup must not overwrite its original source or camera wrapper')
    raw = source.read_bytes()
    digest = sha256(raw)
    if digest != source_sha256.lower():
        raise ValueError(f'Original startup SHA256 mismatch: expected {source_sha256}, got {digest}')
    changed, replacements = render_startup(raw, source_robot_path, wrapper, robot_log_dir)
    profile_patch = {'startup_script': str(destination), 'startup_script_sha256': sha256(changed)}
    patch = ''.join(difflib.unified_diff(raw.decode().splitlines(keepends=True),
                                       changed.decode().splitlines(keepends=True),
                                       fromfile=str(source_robot_path), tofile=str(destination)))
    files = {
        'start_all.sh': changed,
        'profile_patch.json': (json.dumps(profile_patch, indent=2) + '\n').encode(),
        'start_all.diff': patch.encode(),
    }
    manifest = {
        'schema_version': 1,
        'robot': 'F1-09',
        'source': {'local_path': str(source), 'robot_path': str(source_robot_path), 'sha256': digest},
        'destination': str(destination),
        'camera_wrapper': str(wrapper),
        'original_working_directory': str(source_robot_path.parent),
        'robot_log_directory': str(robot_log_dir),
        'camera_replacements': replacements,
        'files': {name: {'sha256': sha256(data), 'bytes': len(data)} for name, data in files.items()},
        'generated_only': True,
        'deployed': False,
        'profile_applied': False,
        'processes_started_or_restarted': False,
        'live_source_recheck_required_before_installation': True,
        'activation': 'Next explicit normal onsite base startup only; never restart the active base-start service to apply this copy.',
        'dependencies': 'Deploy the selected immutable camera wrapper and its FastDDS XML with their release hashes before applying profile_patch.json.',
    }
    files['manifest.json'] = (json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.f1-camera-startup-', dir=output_dir.parent) as temporary:
        staging = Path(temporary) / 'review'
        staging.mkdir()
        for name, data in files.items():
            (staging / name).write_bytes(data)
        # The script is invoked through /bin/bash by StartupService; do not set
        # executable mode or run the generated script as part of preparation.
        staging.rename(output_dir)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--source-robot-path', default='/home/user/workspace/start_all.sh')
    parser.add_argument('--wrapper', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--robot-output-dir', required=True)
    parser.add_argument('--robot-log-dir', default='/home/user/Bruno/DAgger-runtime/base-start-logs')
    args = parser.parse_args()
    try:
        result = prepare(**vars(args))
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps({'output_dir': str(args.output_dir.absolute()), 'destination': result['destination'],
                      'startup_script_sha256': result['files']['start_all.sh']['sha256'],
                      'generated_only': True, 'profile_applied': False}, indent=2))


if __name__ == '__main__':
    main()
