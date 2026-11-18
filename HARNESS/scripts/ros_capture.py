#!/usr/bin/env python3
"""Read-only ROS discovery/capture; runs under robot's native ROS Python, without Harness deps."""
import argparse
import hashlib
import json
from pathlib import Path
import time


def main():
    import rclpy
    from sensor_msgs.msg import Image, CameraInfo, JointState
    from rclpy.qos import qos_profile_sensor_data
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seconds',type=float,default=8)
    args=p.parse_args()
    if not 0 < args.seconds <= 60: p.error('capture duration must be <=60s')
    args.output.mkdir(parents=True,exist_ok=False)
    blobs=args.output/'blobs';blobs.mkdir()
    rclpy.init(args=None)
    node=rclpy.create_node('f1_harness_readonly_capture')
    start=time.monotonic()
    for _ in range(10):rclpy.spin_once(node,timeout_sec=.1)
    topics=node.get_topic_names_and_types()
    records=[];counts={};subscriptions=[]
    def callback(topic,kind):
        def receive(message):
            if counts.get(topic,0)>=3:return
            counts[topic]=counts.get(topic,0)+1
            row=dict(topic=topic,kind=kind,sequence=counts[topic],received_ns=time.monotonic_ns(),
                     source_ns=message.header.stamp.sec*1_000_000_000+message.header.stamp.nanosec,
                     frame_id=message.header.frame_id)
            if kind=='image':
                data=bytes(message.data);sha=hashlib.sha256(data).hexdigest()
                (blobs/sha).write_bytes(data)
                row.update(width=message.width,height=message.height,encoding=message.encoding,
                           row_step_bytes=message.step,is_bigendian=bool(message.is_bigendian),blob_sha256=sha)
            elif kind=='camera_info':
                row.update(k=list(message.k),d=list(message.d),r=list(message.r),p=list(message.p),
                           width=message.width,height=message.height,distortion_model=message.distortion_model)
            else:
                row.update(names=list(message.name),position=list(message.position),
                           velocity=list(message.velocity),effort=list(message.effort))
            records.append(row)
        return receive
    for topic,types in topics:
        kind=message_type=None
        if topic.startswith('/camera/') and types==['sensor_msgs/msg/Image']:
            kind,message_type='image',Image
        elif topic.startswith('/camera/') and types==['sensor_msgs/msg/CameraInfo']:
            kind,message_type='camera_info',CameraInfo
        elif topic=='/hal/joint_states' and types==['sensor_msgs/msg/JointState']:
            kind,message_type='joint_state',JointState
        if message_type:
            subscriptions.append(node.create_subscription(message_type,topic,callback(topic,kind),qos_profile_sensor_data))
    while time.monotonic()-start<args.seconds:rclpy.spin_once(node,timeout_sec=.1)
    report=dict(schema='f1-native-ros-capture-v1',topics=topics,records=records,
                source_clock_verified=False,command_publishers=0)
    (args.output/'capture.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(dict(output=str(args.output),counts=counts,command_publishers=0)))
    node.destroy_node();rclpy.shutdown()


if __name__=='__main__':main()
