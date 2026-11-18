#!/usr/bin/env python3
"""Native ROS camera metadata probe: no control imports, publishers, or retained pixels."""
import argparse
from collections import Counter
import json
from pathlib import Path
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=8)
    args=parser.parse_args()
    if not 1<=args.seconds<=30:parser.error('probe duration must be between 1 and 30 seconds')
    profile=json.loads(args.profile.read_text())
    import rclpy
    from sensor_msgs.msg import Image, CompressedImage
    from rclpy.qos import qos_profile_sensor_data
    args.output.mkdir(parents=True,exist_ok=False)
    rclpy.init(args=None)
    node=rclpy.create_node('f1_harness_camera_timing_readonly')
    rows=[]; counts=Counter(); subscriptions=[]; interrupted=False;overflow=False
    def callback(topic,camera,stream):
        def receive(message):
            nonlocal overflow
            received=time.monotonic_ns()
            counts[topic]+=1
            if len(rows)>=20000:
                overflow=True
                return
            rows.append(dict(kind='image',topic=topic,camera=camera,stream=stream,
                sequence=counts[topic],received_ns=received,
                source_ns=message.header.stamp.sec*1_000_000_000+message.header.stamp.nanosec,
                frame_id=message.header.frame_id))
        return receive
    topics=[]
    for camera in profile['cameras']:
        for stream in ('rgb','depth'):
            spec=camera[stream];topic=spec['topic'];topics.append(topic)
            msg=CompressedImage if spec['type']=='compressed' else Image
            subscriptions.append(node.create_subscription(msg,topic,
                callback(topic,camera['name'],stream),qos_profile_sensor_data))
    started_unix=time.time_ns();start=time.monotonic_ns()
    try:
        while time.monotonic_ns()-start<args.seconds*1e9:
            rclpy.spin_once(node,timeout_sec=.1)
    except KeyboardInterrupt:
        interrupted=True
    finally:
        finish=time.monotonic_ns()
        report=dict(schema='f1-camera-metadata-probe-v1',records=rows,
            host_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            started_unix_ns=started_unix,window_start_received_ns=start,window_end_received_ns=finish,
            requested_seconds=args.seconds,complete_window=not interrupted and not overflow,
            interrupted=interrupted,metadata_only=True,overflow=overflow,image_interval_s=0,
            topics=topics,received_counts=dict(counts),retained_counts=dict(counts),
            missing_topics=sorted(set(topics)-set(counts)),
            source_clock_verified=False,command_publishers=0,motion_authorized=False)
        (args.output/'capture.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(dict(output=str(args.output),frames=len(rows),counts=dict(counts),
                             missing_topics=report['missing_topics'],command_publishers=0)))
        node.destroy_node();rclpy.shutdown()
    return 0 if not interrupted and not overflow and not report['missing_topics'] else 2


if __name__=='__main__':
    raise SystemExit(main())
