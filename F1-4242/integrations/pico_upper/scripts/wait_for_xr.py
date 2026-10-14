#!/usr/bin/env python3
"""No motion: wait for late PICO connection without repeatedly closing the SDK."""
import time
import xrobotoolkit_sdk as xrt
from pico_f1.xr_readiness import XrReadiness


def main():
    gate = XrReadiness()
    xrt.init()
    print('WAITING_FOR_PICO connect headset, select Head + Controller and Send; '
          'release grips/buttons/triggers and center sticks. Ctrl+C or ./stop.sh cancels.', flush=True)
    report = 0
    try:
        while True:
            now = time.monotonic()
            try:
                poses = [xrt.get_left_controller_pose(), xrt.get_right_controller_pose(), xrt.get_headset_pose()]
                axes = [*xrt.get_left_axis(), *xrt.get_right_axis()]
                neutral = (all(abs(float(v)) <= .05 for v in axes)
                           and all(float(fn()) <= .05 for fn in (
                               xrt.get_left_grip, xrt.get_right_grip,
                               xrt.get_left_trigger, xrt.get_right_trigger))
                           and not any(fn() for fn in (xrt.get_A_button, xrt.get_B_button,
                               xrt.get_X_button, xrt.get_Y_button, xrt.get_left_menu_button)))
                ready = gate.update(int(xrt.get_time_stamp_ns()), poses, neutral, now)
            except Exception as exc:
                gate.update(0, [], False, now)
                ready = False
                if now >= report:
                    print('XR_READ_RETRY ' + str(exc), flush=True)
            if ready:
                print('XR_LIVE_READY fresh_timestamps=true neutral_controls=true', flush=True)
                return 0
            if now >= report:
                print(gate.reason + ' (no robot command processes started)', flush=True)
                report = now + 5
            time.sleep(.02)
    finally:
        xrt.close()


if __name__ == '__main__':
    raise SystemExit(main())
