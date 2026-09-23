"""Loopback sender. No scalar shortcuts, reset, waist or recording protocol."""
import socket
import struct
import numpy as np
from pico_upper.contract import UDP_HOST, UDP_PORT, validate_frame

class UpperSender:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
    def send_upper_to_sim(self, values):
        q = validate_frame(values)
        try:
            self.sock.sendto(struct.pack('<18d', *q), (UDP_HOST, UDP_PORT))
        except BlockingIOError:
            pass
    def send_start_video_signal(self, values):
        raise RuntimeError('Recording disabled: this trial does not change collection')
    def home_position(self):
        raise RuntimeError('XY reset is disabled in upper-only trial')
    def fun_send(self, values):
        raise RuntimeError('Legacy waist/reset shortcut is disabled')
    def angle_map(self, left_joint_rad, right_joint_rad):
        # Unchanged upstream model-to-driver arm mapping.
        l, r = np.rad2deg(left_joint_rad), np.rad2deg(right_joint_rad)
        return (l * np.array([1,-1,1,-1,1,-1,1]) + np.array([90,-90,-90,-90,0,0,0]),
                r * np.array([1,1,1,1,-1,-1,1]) + np.array([-90,-90,90,-90,0,0,0]))
    def __del__(self):
        if hasattr(self, 'sock'):
            self.sock.close()
