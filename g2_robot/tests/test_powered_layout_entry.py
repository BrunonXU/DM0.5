"""Exercise actual Shell argument construction without GDK, network or motion."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from g2_sort.deployment.g2_client.cli import parse_args


@pytest.mark.parametrize('entry,layout', [
    ('19_powered_sync_dual_side_17d.sh','dual-side-17d'),
    ('22_powered_sync_unified_34.sh','unified-34'),
])
@pytest.mark.parametrize('micro_profile', ['stationary_minjerk_v1', 'off'])
def test_entry_arguments_parse_with_identical_control_profile(tmp_path,entry,layout,micro_profile):
    scripts=Path(__file__).resolve().parents[1]/'scripts'
    (tmp_path/'lib').mkdir()
    shutil.copy2(scripts/entry,tmp_path/entry)
    shutil.copy2(scripts/'lib/powered_split_runner.sh',tmp_path/'lib/powered_split_runner.sh')
    preflight=tmp_path/'11_rtc_process_isolation_preflight.sh'
    preflight.write_text('#!/bin/bash\nexit 0\n');preflight.chmod(0o755)
    (tmp_path/'lib/common.sh').write_text('''
section() { :; }
assert_no_controller_process() { :; }
check_policy_endpoint() { :; }
load_g2_environment() { :; }
sleep() { :; }
timestamp() { echo test; }
G2_POWERED_LOG_DIR=/tmp
python3.12() { printf '%s\\0' "$@" > "$ARG_CAPTURE"; }
''')
    capture=tmp_path/'args'
    result=subprocess.run(['bash',str(tmp_path/entry),'--duration-seconds','60',
        '--arm-micro-motion-profile',micro_profile],
        input='ok\n',text=True,capture_output=True,
        env={**os.environ,'ARG_CAPTURE':str(capture)},timeout=10)
    assert result.returncode==0,result.stdout+result.stderr
    raw=capture.read_bytes().decode().split('\0')[:-1]
    assert raw[:2]==['-m','g2_sort.deployment.g2_sdk_client']
    args=parse_args(raw[2:])
    assert args.servo_output_mode==layout
    assert args.servo_channel=='normal'
    assert args.servo_timing_warning_only is (layout == "unified-34")
    assert args.split_control_profile is True
    assert args.rtc_process_isolation is True
    assert args.boundary_blend_seconds==.12
    assert args.arm_internal_jerk_weight==10
    assert args.arm_interpolation_profile=='cubic_spline'
    assert args.arm_micro_motion_profile==micro_profile
    assert args.arm_micro_motion_threshold_rad==.0035
    assert args.hand_smoothing_profile=='off'


def test_unified_profile_rejects_low_latency():
    with pytest.raises(SystemExit):
        parse_args(['--mode','powered-sync','--split-control-profile',
                    '--servo-output-mode','unified-34','--servo-channel','low-latency'])


def test_timing_warning_only_rejects_dual17():
    with pytest.raises(SystemExit):
        parse_args(['--mode','powered-sync','--servo-output-mode','dual-side-17d',
                    '--servo-channel','normal','--servo-timing-warning-only'])
