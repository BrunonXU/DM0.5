#!/usr/bin/env python3
"""Prepare one pinned 192 onsite test; no network or robot actions."""
from pathlib import Path
import json
import shutil
import sys
import tarfile
from build_arm_guided_joint_proposals import TASK,sha,write
sys.path.insert(0,str(TASK/'robot'))
from f1_replay.bundle import load_bundle
from f1_replay.runtime import validate_actions

ROOT='/home/user/Bruno/F1/foam_sort_whole_body'
VERSION='20260917-demo-event-complete-v1'

def main():
    run=TASK/'results/demo_training_release_20260917_v1';out=run/'replay_192';payload=out/'payload'
    release=payload/'releases'/VERSION;release.mkdir(parents=True,exist_ok=False)
    shutil.copytree(TASK/'robot/f1_replay',release/'robot/f1_replay',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copytree(out/'bundle',release/'bundle')
    (release/'configs').mkdir()
    profile=json.loads((TASK/'results/replay_retiming_057_20260916/replay_f1.json').read_text())
    profile['diagnostic_note']='192 final demo check: same source clock and amplitude-limited labels as training source; no retiming. Physical stop/material success pending onsite test.'
    write(release/'configs/replay_f1.json',profile)
    old=TASK/'results/deployments/20260916-recorder-ready-v1/payload/releases/20260916-recorder-ready-v1/workspace/F1/foam_sort_whole_body'
    pose_profile=json.loads((old/'configs/pose_replay_f1.json').read_text())
    pose_profile['base_controller_expected_parameters']=profile['base_controller_expected_parameters']
    write(release/'configs/pose_replay_f1.json',pose_profile)
    pose=json.loads((TASK/'results/base_home_delivery_20260917/payload/releases/20260917-base-home-v1/configs/pose_192.json').read_text())
    pose['bundle']=ROOT+'/releases/'+VERSION+'/bundle'
    pose['profile']=ROOT+'/releases/'+VERSION+'/configs/pose_replay_f1.json'
    write(release/'configs/pose_192.json',pose)
    manifest=json.loads((release/'bundle/manifest.json').read_text())
    manifest['status']='replay_ready'
    manifest['execution_contract']={k:profile[k] for k in ['arm_limit_policy','controller_binary_sha256']}
    manifest['acceptance'].update(robot_replay_verified=False,training_reader_verified=False,
        note='Approved only as an onsite diagnostic with original reviewed phase timing and bounded-amplitude source labels; not physical success or training approval.',
        source_selection_sha256=sha(run/'selection.json'),base_limits_reference='base_controller_review_20260916; live parameters must match before execute')
    write(release/'bundle/manifest.json',manifest)
    bundle=load_bundle(release/'bundle');validate_actions(bundle,profile)
    # The prepared pose differs only in the explicitly selected bundle/profile.
    # Live pose and control ownership are still checked by existing clients.
    wrapper='''#!/usr/bin/env bash
set -eo pipefail
root=/home/user/Bruno/F1/foam_sort_whole_body
release="$root/releases/20260917-demo-event-complete-v1"
mode="${1:-inspect}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$release/robot${PYTHONPATH:+:$PYTHONPATH}"
case "$mode" in inspect|dry-run) ;; preflight|execute|status|capture|plan-start|to-start)
  source /opt/ros/jazzy/setup.bash
  source /home/user/workspace/f1/install/setup.bash
  source /home/user/workspace/agv/install/setup.bash
  export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  export CYCLONEDDS_URI=/etc/ros/cyclonedds/cyclonedds.xml
  export ROS_LOG_DIR="$root/runtime/replay/ros_logs"
  ;; *) echo 'Modes: inspect dry-run status capture plan-start to-start preflight execute (192 only)' >&2; exit 2;; esac
case "$mode" in status|capture|plan-start|to-start)
  exec /usr/bin/python3 -m f1_replay.pose_cli "$mode" --config "$release/configs/pose_192.json";;
  *) exec /usr/bin/python3 -m f1_replay.cli "$mode" --bundle "$release/bundle" --profile "$release/configs/replay_f1.json" --output "$root/runtime/replay_final_192";; esac
'''
    entry=payload/'replay_final_192.sh';entry.write_text(wrapper);entry.chmod(0o755)
    manifest=dict(root=ROOT,version=VERSION,robot_motion_sent=False,training_approved=False,
                  files={str(p.relative_to(payload)):{'sha256':sha(p),'size_bytes':p.stat().st_size} for p in payload.rglob('*') if p.is_file()},
                  source_npz_sha256=manifest['source']['aligned_npz']['sha256'])
    write(out/'deployment_manifest.json',manifest)
    with tarfile.open(out/'release.tar.gz','w:gz') as archive:
        for name in manifest['files']:archive.add(payload/name,arcname=name,recursive=False)
    write(out/'offline_bundle_check.json',dict(status='passed',frames=len(bundle.frames),duration_s=bundle.duration_ns/1e9,
        all_actions_pass_profile=True,package_sha256=sha(out/'release.tar.gz'),robot_replay_verified=False,
        training_source_sha256=manifest['source_npz_sha256']))
    print(json.dumps(dict(frames=len(bundle.frames),duration_s=bundle.duration_ns/1e9,package=str(out/'release.tar.gz'))))

if __name__=='__main__':main()
