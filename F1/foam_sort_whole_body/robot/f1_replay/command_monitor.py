"""Read-only native ROS subscriber bridge for Jazzy's missing Python publisher GID."""
from pathlib import Path
import hashlib
import os
import subprocess
import time


def publisher_gid(publisher):
    """Use public RCL/RMW APIs; Cyclone graph GUID is not its message-info GID.

    ABI verified against F1 Jazzy headers: rmw_gid_t = identifier + 16 bytes.
    Hold the rclpy handle alive for both calls; no control message is sent.
    """
    import ctypes as C
    class Gid(C.Structure):
        _fields_=[('implementation_identifier',C.c_char_p),('data',C.c_uint8*16)]
    rcl=C.CDLL('/opt/ros/jazzy/lib/librcl.so')
    rmw=C.CDLL('/opt/ros/jazzy/lib/librmw_cyclonedds_cpp.so')
    rcl.rcl_publisher_get_rmw_handle.argtypes=[C.c_void_p]
    rcl.rcl_publisher_get_rmw_handle.restype=C.c_void_p
    rmw.rmw_get_gid_for_publisher.argtypes=[C.c_void_p,C.POINTER(Gid)]
    rmw.rmw_get_gid_for_publisher.restype=C.c_int
    gid=Gid()
    with publisher.handle:
        ptr=rcl.rcl_publisher_get_rmw_handle(publisher.handle.pointer)
        if not ptr or rmw.rmw_get_gid_for_publisher(ptr,C.byref(gid))!=0:
            raise RuntimeError('无法读取自身发布者RMW身份')
    if not gid.implementation_identifier or b'cyclonedds' not in gid.implementation_identifier or not any(gid.data):
        raise RuntimeError('发布者RMW身份无效或不是已核对的CycloneDDS')
    return bytes(gid.data).hex()


class CommandMonitor:
    def __init__(self, profile, name, on_command):
        import ctypes as C
        from collections import deque
        path=Path(profile['command_monitor_binary'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=profile['command_monitor_sha256']:
            raise RuntimeError('只读命令监听器SHA256不匹配')
        self.on_command=on_command
        self.started_ns=None
        self.heartbeat_ns=None
        self.events=deque()
        self.overflow=False
        self.library=C.CDLL(str(path))
        callback_type=C.CFUNCTYPE(None,C.c_char_p)
        def receive(raw):
            if len(self.events)>=4096:self.overflow=True
            else:self.events.append((time.monotonic_ns(),raw.decode('ascii')))
        self.callback=callback_type(receive)
        self.library.f1_monitor_start.argtypes=[callback_type]+[C.c_char_p]*5
        self.library.f1_monitor_start.restype=C.c_void_p
        self.library.f1_monitor_stop.argtypes=[C.c_void_p]
        self.library.f1_monitor_stop.restype=None
        keys=('arms_command','left_gripper_command','right_gripper_command','base_command')
        self.handle=self.library.f1_monitor_start(self.callback,name.encode(),*[profile['topics'][k].encode() for k in keys])
        if not self.handle:raise RuntimeError('只读命令监听器启动失败')

    def poll(self):
        if not self.handle:raise RuntimeError('命令监听器已关闭')
        if self.overflow:raise RuntimeError('命令监听队列溢出，拒绝继续动作')
        for _ in range(4096):
            if not self.events:break
            received,line=self.events.popleft()
            self.accept(line,received)
        if self.heartbeat_ns is not None and time.monotonic_ns()-self.heartbeat_ns>1_000_000_000:
            raise RuntimeError('命令监听器心跳超时，不能继续动作')

    def accept(self,line,received=None):
        now=time.monotonic_ns() if received is None else received
        if line=='READY':self.started_ns=now;self.heartbeat_ns=now
        elif line=='ALIVE':self.heartbeat_ns=now
        elif line.startswith('CMD '):
            _,key,gid=line.split()
            if key not in ('arms_command','left_gripper_command','right_gripper_command','base_command') or len(gid)!=32:
                raise RuntimeError('命令监听记录格式错误')
            bytes.fromhex(gid)
            self.on_command(key,gid)
        else:raise RuntimeError('命令监听器错误：'+line)

    def close(self):
        if self.handle:
            self.library.f1_monitor_stop(self.handle)
            self.handle=None
