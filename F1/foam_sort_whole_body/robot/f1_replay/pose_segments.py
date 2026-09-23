"""Feedback gates for generated pose preparation; never used for data replay."""
import time
from .bundle import ARM_NAMES, BODY_NAMES
from .runtime import ReplayError


def segment_end_indices(frames, stride):
    """Keep original smooth waypoints; end segments on full-output samples."""
    if not frames or stride < 1 or (len(frames)-1) % stride:
        raise ReplayError('分段起姿终点必须对齐辅助输出周期')
    bounds = [1.] * 14 + [.25] * 5 + [5.] * 2
    result = []
    start = 0
    for index in range(stride, len(frames), stride):
        if any(abs(a-b) > bound+1e-9 for a,b,bound in
               zip(frames[index].action,frames[start].action,bounds)):
            end = index-stride
            if end == start:
                raise ReplayError('单个采样周期超过分段起姿步长')
            result.append(end)
            start = end
            if any(abs(a-b) > bound+1e-9 for a,b,bound in
                   zip(frames[index].action,frames[start].action,bounds)):
                raise ReplayError('单个采样周期超过分段起姿步长')
    if not result or result[-1] != len(frames)-1:
        result.append(len(frames)-1)
    return result


def wait_pose_segment(read_errors, hold, log, *, hz, body_tolerance,
                      gripper_tolerance, timeout_s=10., max_stall_s=.2,
                      now=time.monotonic, sleep=time.sleep):
    """Hold one endpoint at the original cadence until feedback settles."""
    begin=now();stable=None;last=begin;count=0
    while True:
        errors=read_errors()  # Freshness, modes, ownership and cancellation remain checked.
        current=now()
        if current-last > max_stall_s:
            raise ReplayError('分段起姿保持期间调度卡顿')
        reached=(all(abs(errors['joints'][n]) <= .25 for n in ARM_NAMES)
                 and all(abs(errors['joints'][n]) <= body_tolerance for n in BODY_NAMES)
                 and all(abs(v) <= gripper_tolerance for v in errors['grippers'].values()))
        if reached:
            if stable is None:stable=current
            if current-stable >= .15:
                log('segment_arrived',{'elapsed_s':current-begin,'errors':errors})
                return
        else:stable=None
        if current-begin >= timeout_s:
            error,name=max((abs(errors['joints'][n]),n) for n in ARM_NAMES+BODY_NAMES)
            raise ReplayError(f'分段起姿未到位：{name}距本段目标{error:.3f}°；已等待{timeout_s:g}秒，未发送后续段')
        sleep(max(0, 1/hz-(current-last)))
        read_errors()
        if now()-last > max_stall_s:
            raise ReplayError('分段起姿保持期间调度卡顿')
        last=now()
        hold(count)
        count+=1
