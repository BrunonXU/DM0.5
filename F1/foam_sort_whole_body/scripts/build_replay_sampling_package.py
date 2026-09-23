#!/usr/bin/env python3
"""Prepare three continuous, source-bound onsite diagnostic replays."""
import json
from pathlib import Path
import shutil
import sys
import tarfile
import numpy as np
from export_replay_bundle import export_bundle, TASK
from build_arm_guided_joint_proposals import sha, write
sys.path.insert(0,str(TASK/'robot'))
from f1_replay.bundle import load_bundle
from f1_replay.runtime import validate_actions
from f1_replay.pose_model import start_target

VERSION='20260917-fullpool-samples-v1'
REMOTE='/home/user/Bruno/F1/foam_sort_whole_body'


def main():
    out=TASK/'results/replay_sampling_20260917_v1';payload=out/'payload'
    release=payload/'releases'/VERSION;release.mkdir(parents=True,exist_ok=False)
    (out/'start_targets').mkdir()
    baseline=TASK/'results/demo_training_release_20260917_v1/replay_192/payload/releases/20260917-demo-event-complete-v1'
    shutil.copytree(baseline/'robot',release/'robot')
    shutil.copytree(baseline/'configs',release/'configs')
    profile=json.loads((release/'configs/replay_f1.json').read_text())
    pose_profile=json.loads((release/'configs/pose_replay_f1.json').read_text())
    records=[]
    for sample,batch in [('020','all_data_1_derived'),('096','all_data_1_derived'),('093','all_data_derived')]:
        ep=f'{batch}/episode_000{sample}';stem=ep.replace('/','_')
        source=TASK/'results/full_pool_reader_20260917_v1/cases'/stem/'composed/episode.npz'
        meta=json.loads(source.with_suffix('.json').read_text())
        with np.load(source,allow_pickle=False)as z:d={k:z[k]for k in z.files}
        good=d['state_valid'].all(1)&d['action_candidate_valid'].all(1)
        edges=np.diff(np.r_[False,good,False].astype(int))
        runs=list(zip(np.flatnonzero(edges==1),np.flatnonzero(edges==-1)))
        events=sum([meta['event_coverage'][k]['indices']for k in ['grasp','release']],[])
        choices=[(a,b)for a,b in runs if a<=min(events) and max(events)<b and b<len(good)]
        assert len(choices)==1
        a,b=map(int,choices[0]);bundlepath=release/'bundles'/sample
        export_bundle(source,bundlepath,meta['cleaning_version'],a,b)
        manifest=json.loads((bundlepath/'manifest.json').read_text());manifest['status']='replay_ready'
        manifest['execution_contract']={k:profile[k]for k in ['arm_limit_policy','controller_binary_sha256']}
        manifest['acceptance'].update(robot_replay_verified=False,training_reader_verified=True,
            note='Onsite sample check only; same source clock/labels as the engineering-bound 740 pool; excludes unsupervised reset.',
            engineering_release_sha256=sha(TASK/'results/full_pool_reader_20260917_v1/engineering_release.json'))
        write(bundlepath/'manifest.json',manifest)
        bundle=load_bundle(bundlepath);validate_actions(bundle,profile)
        oldpose=TASK/'results/pose_start_intersection_20260917/payload/releases/20260917-pose-start-intersection-v1/configs'/f'pose_{sample}.json'
        pose=json.loads(oldpose.read_text());pose.update(bundle=f'{REMOTE}/releases/{VERSION}/bundles/{sample}',
            profile=f'{REMOTE}/releases/{VERSION}/configs/pose_replay_f1.json')
        target=start_target(bundle.frames[0],pose_profile,policy=pose.get('start_pose_policy','recorded_hal'))
        write(out/'start_targets'/f'{sample}.json',target)
        write(release/'configs'/f'pose_{sample}.json',pose)
        records.append(dict(sample=sample,episode=ep,frames=len(bundle.frames),duration_s=bundle.duration_ns/1e9,
            source_start_index=a,source_stop_index=b,source_npz_sha256=sha(source),onsite_passed=False))
    wrapper='''#!/usr/bin/env bash
set -eo pipefail
root=/home/user/Bruno/F1/foam_sort_whole_body
release="$root/releases/20260917-fullpool-samples-v1"
mode="${1:-inspect}"
sample="${2:-020}"
case "$sample" in 020|096|093) ;; *) echo 'Samples: 020 096 093' >&2; exit 2;; esac
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$release/robot${PYTHONPATH:+:$PYTHONPATH}"
case "$mode" in inspect|dry-run) ;; status|plan-start|to-start|preflight|execute)
 source /opt/ros/jazzy/setup.bash
 source /home/user/workspace/f1/install/setup.bash
 source /home/user/workspace/agv/install/setup.bash
 export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
 export CYCLONEDDS_URI=/etc/ros/cyclonedds/cyclonedds.xml
 export ROS_LOG_DIR="$root/runtime/replay_sampling/ros_logs"
 ;; *) echo 'Modes: inspect dry-run status plan-start to-start preflight execute' >&2; exit 2;; esac
case "$mode" in status|plan-start|to-start)
 exec /usr/bin/python3 -m f1_replay.pose_cli "$mode" --config "$release/configs/pose_$sample.json";;
 *) exec /usr/bin/python3 -m f1_replay.cli "$mode" --bundle "$release/bundles/$sample" --profile "$release/configs/replay_f1.json" --output "$root/runtime/replay_sampling/$sample";; esac
'''
    entry=payload/'replay_samples.sh';entry.write_text(wrapper);entry.chmod(0o755)
    manifest=dict(version=VERSION,remote_root=REMOTE,motion_sent=False,training_approved=False,samples=records,
        files={str(p.relative_to(payload)):dict(sha256=sha(p),size=p.stat().st_size)for p in payload.rglob('*')if p.is_file()})
    write(out/'deployment_manifest.json',manifest)
    with tarfile.open(out/'release.tar.gz','w:gz')as archive:
        for name in manifest['files']:archive.add(payload/name,arcname=name,recursive=False)
    print(json.dumps(records,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
