"""Read-only F710 input probe. No sockets, ROS, transport, or robot commands.

Run using the same Python as windows_f1_sender.cmd. Raw SDL, the installed
reader/mapper, and XInputGetState are sampled together; neither button mappings
nor existing SDL environment hints are changed. The local JSON preserves every
sample even when console output is rate/line limited.
"""
from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time


CLIENT_ROOT = Path(__file__).resolve().parents[1]
ENV_KEYS = (
    "SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "SDL_JOYSTICK_THREAD",
    "SDL_JOYSTICK_HIDAPI", "SDL_JOYSTICK_RAWINPUT", "SDL_XINPUT_ENABLED",
    "SDL_GAMECONTROLLERCONFIG", "PYTHONPATH",
)
XINPUT_BUTTONS = {
    0x0001: "DPAD_UP", 0x0002: "DPAD_DOWN", 0x0004: "DPAD_LEFT",
    0x0008: "DPAD_RIGHT", 0x0010: "START", 0x0020: "BACK",
    0x0040: "LEFT_THUMB", 0x0080: "RIGHT_THUMB", 0x0100: "LB",
    0x0200: "RB", 0x1000: "A", 0x2000: "B", 0x4000: "X", 0x8000: "Y",
}


class XInputGamepad(ctypes.Structure):
    _fields_ = [("buttons", ctypes.c_uint16), ("left_trigger", ctypes.c_uint8),
                ("right_trigger", ctypes.c_uint8), ("left_x", ctypes.c_int16),
                ("left_y", ctypes.c_int16), ("right_x", ctypes.c_int16),
                ("right_y", ctypes.c_int16)]


class XInputState(ctypes.Structure):
    _fields_ = [("packet", ctypes.c_uint32), ("gamepad", XInputGamepad)]


class ReadOnlyXInput:
    """Only resolves XInputGetState; never resolves any output API."""
    def __init__(self):
        self.dll_name = None
        self.error = None
        self._get_state = None
        if sys.platform != "win32":
            self.error = "XInput unavailable on this platform"
            return
        failures = []
        for name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
            try:
                self._dll = ctypes.WinDLL(name)
                function = self._dll.XInputGetState
                function.argtypes = [ctypes.c_uint32, ctypes.POINTER(XInputState)]
                function.restype = ctypes.c_uint32
                self._get_state, self.dll_name = function, name
                return
            except (OSError, AttributeError) as exc:
                failures.append(f"{name}: {exc}")
        self.error = "; ".join(failures)

    def sample(self):
        if self._get_state is None:
            return []
        result = []
        for slot in range(4):
            state = XInputState()
            status = int(self._get_state(slot, ctypes.byref(state)))
            item = {"slot": slot, "status": status, "connected": status == 0}
            if status == 0:
                gamepad = state.gamepad
                item.update(packet=int(state.packet), buttons=int(gamepad.buttons),
                    pressed=[name for mask, name in XINPUT_BUTTONS.items()
                             if int(gamepad.buttons) & mask],
                    axes=[int(gamepad.left_x), int(gamepad.left_y),
                          int(gamepad.right_x), int(gamepad.right_y)],
                    triggers=[int(gamepad.left_trigger), int(gamepad.right_trigger)])
            result.append(item)
        return result


def file_fingerprint(path):
    path = Path(path).resolve()
    try:
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    except OSError as exc:
        return {"path": str(path), "error": str(exc)}


def device_info(joystick, index):
    info = {"index": index}
    for key, method in (("name", "get_name"), ("guid", "get_guid"),
                        ("instance_id", "get_instance_id"), ("axes", "get_numaxes"),
                        ("buttons", "get_numbuttons"), ("hats", "get_numhats")):
        try:
            info[key] = getattr(joystick, method)()
        except Exception as exc:
            info[key + "_error"] = str(exc)
    return info


def raw_sdl_sample(reader):
    # reader.snapshot() has already pumped/drained SDL events this iteration.
    js = reader._joystick
    if js is None:
        return {"error": "reader has no open SDL joystick"}
    try:
        return {"buttons": [int(js.get_button(i)) for i in range(js.get_numbuttons())],
                "axes": [float(js.get_axis(i)) for i in range(js.get_numaxes())],
                "hats": [list(js.get_hat(i)) for i in range(js.get_numhats())]}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def visible_key(sample):
    raw = sample["sdl"]
    # Ignore tiny stick jitter and XInput packet ticks in console change lines.
    return (tuple(raw.get("buttons", ())),
            tuple(round(v * 8) for v in raw.get("axes", ())),
            tuple(tuple(v) for v in raw.get("hats", ())), raw.get("error"),
            tuple((v["slot"], v["status"], v.get("buttons"),
                   tuple(round(a / 4096) for a in v.get("axes", ())))
                  for v in sample["xinput"]),
            sample.get("reader_error"), json.dumps(sample.get("mapped"), sort_keys=True))


def summarize(samples):
    sdl_buttons = set()
    hats = set()
    connected_samples = start_edges = 0
    previous_start = False
    toggles = []
    slots = {i: {"connected_samples": 0, "start_rising_edges": 0,
                 "button_names_seen": set()} for i in range(4)}
    previous_xstart = {i: False for i in range(4)}
    for item in samples:
        raw = item["sdl"]
        sdl_buttons.update(i for i, value in enumerate(raw.get("buttons", [])) if value)
        hats.update(tuple(hat) for hat in raw.get("hats", []))
        actual = item.get("reader", {})
        connected_samples += bool(actual.get("connected"))
        start = bool(actual.get("start"))
        start_edges += start and not previous_start
        previous_start = start
        if "mapped" in item:
            toggles.append(item["mapped"]["hil_toggle_id"])
        for state in item["xinput"]:
            slot = state["slot"]
            slots[slot]["connected_samples"] += bool(state["connected"])
            names = state.get("pressed", [])
            slots[slot]["button_names_seen"].update(names)
            pressed = "START" in names
            slots[slot]["start_rising_edges"] += pressed and not previous_xstart[slot]
            previous_xstart[slot] = pressed
    for item in slots.values():
        item["button_names_seen"] = sorted(item["button_names_seen"])
    axis_count = max((len(item["sdl"].get("axes", [])) for item in samples), default=0)
    axis_ranges = []
    for index in range(axis_count):
        values = [item["sdl"]["axes"][index] for item in samples
                  if len(item["sdl"].get("axes", [])) > index]
        axis_ranges.append({"index": index, "min": min(values), "max": max(values)})
    raw_keys = [json.dumps(item["sdl"], sort_keys=True) for item in samples]
    elapsed = [item["elapsed_s"] for item in samples]
    gaps = [b-a for a, b in zip(elapsed, elapsed[1:])]
    return {"samples": len(samples), "reader_connected_samples": connected_samples,
            "reader_start_rising_edges": start_edges,
            "mapper_toggle_max": max(toggles, default=0),
            "sdl_buttons_seen": sorted(sdl_buttons),
            "sdl_hats_seen": sorted(hats), "sdl_axis_ranges": axis_ranges,
            "sdl_raw_changes": sum(a != b for a, b in zip(raw_keys, raw_keys[1:])),
            "xinput_slots": slots,
            "max_sample_gap_ms": round(max(gaps, default=0) * 1000, 2),
            "reader_errors": sum("reader_error" in item for item in samples),
            "interpretation": "observations only; no automatic cause assigned"}


def collect_samples(reader, mapper, xinput, seconds, samples, *,
                    clock=time.monotonic, sleep=time.sleep, emit=print):
    prompts = [(0, "释放全部按键、扳机和摇杆 2 秒。"),
               (2, "按住 START 约 0.5 秒后释放，重复两次；不必同时按 RB。"),
               (9, "依次按一下十字键左、右，每次按后释放。"),
               (16, "按住 RB，小幅移动摇杆，然后全部释放。"),
               (23, "最后再按放一次 START，然后保持全部释放。")]
    started = clock()
    deadline = started
    prompt_index = shown_changes = 0
    previous = None
    last_console = -1.0
    while True:
        elapsed = clock() - started
        if elapsed >= seconds:
            break
        while prompt_index < len(prompts) and elapsed >= prompts[prompt_index][0]:
            emit(f"[{elapsed:5.1f}s] {prompts[prompt_index][1]}")
            prompt_index += 1
        item = {"elapsed_s": round(elapsed, 6)}
        try:
            snapshot = reader.snapshot()
            item["reader"] = asdict(snapshot)
            item["mapped"] = asdict(mapper.map(snapshot))
        except Exception as exc:
            item["reader_error"] = f"{type(exc).__name__}: {exc}"
        item["sdl"] = raw_sdl_sample(reader)
        item["xinput"] = xinput.sample()
        samples.append(item)
        key = visible_key(item)
        if key != previous and shown_changes < 20 and elapsed - last_console >= .35:
            actual, mapped = item.get("reader", {}), item.get("mapped", {})
            raw = item["sdl"]
            pressed = [i for i, value in enumerate(raw.get("buttons", [])) if value]
            xi = [(v["slot"], v.get("pressed", [])) for v in item["xinput"] if v["connected"]]
            emit(f"[{elapsed:5.1f}s] SDL buttons={pressed} hats={raw.get('hats')} "
                 f"axes={[round(v, 2) for v in raw.get('axes', [])]} "
                 f"reader.connected={actual.get('connected')} START={actual.get('start')} "
                 f"toggle={mapped.get('hil_toggle_id')} side={mapped.get('active_side')} "
                 f"RB={actual.get('rb')} XInput={xi}")
            shown_changes += 1
            last_console = elapsed
            previous = key
        deadline += .02
        sleep(max(0., deadline - clock()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=28.)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.index < 0 or not 1 <= args.seconds <= 30:
        parser.error("index must be nonnegative; seconds must be in [1, 30]")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output or CLIENT_ROOT / "logs" /
              f"f1_input_probe_{stamp}_{os.getpid()}.json").resolve()
    report = {"schema": "f1_windows_input_probe_v1", "started_utc": stamp,
              "network": False, "robot_commands": False, "python": sys.executable,
              "python_version": sys.version, "platform": sys.platform,
              "selected_joystick_index": args.index, "duration_requested_s": args.seconds,
              "environment_before_reader_open": {k: os.environ.get(k) for k in ENV_KEYS},
              "samples": []}
    reader = None
    exit_code = 0
    print("F1 输入只读检查：无网络连接、无机器人指令，不需要运行 F1。")
    print(f"Python: {sys.executable}; SDL index={args.index}; JSON: {output}")
    try:
        # The .cmd launcher puts this exact directory first on PYTHONPATH.
        sys.path.insert(0, str(CLIENT_ROOT / "src"))
        reader_module = importlib.import_module("dagger_core.gamepad_windows")
        mapper_module = importlib.import_module("dagger_core.f710_mapping")
        report["import_paths"] = {"reader": reader_module.__file__, "mapper": mapper_module.__file__}
        paths = [Path(__file__), Path(reader_module.__file__), Path(mapper_module.__file__)]
        paths += [CLIENT_ROOT / "scripts" / name for name in
                  ("03_windows_f1_sender.py", "03_windows_shadow_sender.py", "windows_f1_sender.cmd")]
        paths += [CLIENT_ROOT / "src/g2dagger" / name for name in
                  ("async_transport.py", "protocol.py", "sender_guidance.py")]
        report["files"] = [file_fingerprint(path) for path in paths]
        for name, module in (("reader", reader_module), ("mapper", mapper_module)):
            info = file_fingerprint(module.__file__)
            print(f"{name}: {info['path']}; SHA256={info.get('sha256')}")
        reader = reader_module.WindowsF710Reader(joystick_index=args.index)
        mapper = mapper_module.F710Mapper()
        reader.open()
        report["environment_after_reader_open"] = {k: os.environ.get(k) for k in ENV_KEYS}
        report["reader_layout"] = asdict(reader.layout)
        pygame = reader._pygame
        report["pygame_version"] = pygame.version.ver
        report["sdl_version"] = list(pygame.get_sdl_version())
        devices = []
        for index in range(pygame.joystick.get_count()):
            device = pygame.joystick.Joystick(index)
            device.init()
            devices.append(device_info(device, index))
        report["sdl_devices"] = devices
        print("SDL devices: " + json.dumps(devices, ensure_ascii=False))
        print("SDL hints before/after: " + json.dumps({k: [report['environment_before_reader_open'][k],
              report['environment_after_reader_open'][k]] for k in ENV_KEYS[:5]}, ensure_ascii=False))
        xinput = ReadOnlyXInput()
        report["xinput"] = {"dll": xinput.dll_name, "error": xinput.error,
                            "api": "XInputGetState only"}
        print("XInput: " + json.dumps(report["xinput"], ensure_ascii=False))
        collect_samples(reader, mapper, xinput, args.seconds, report["samples"])
    except KeyboardInterrupt:
        report["interrupted"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print("Probe error: " + report["error"])
        exit_code = 1
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception as exc:
                report["close_error"] = str(exc)
        report["summary"] = summarize(report["samples"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary = report["summary"]
        print(f"完成 samples={summary['samples']} reader START edges={summary['reader_start_rising_edges']} "
              f"mapper toggle={summary['mapper_toggle_max']} SDL buttons={summary['sdl_buttons_seen']}")
        print("XInput summary: " + json.dumps(summary["xinput_slots"], ensure_ascii=False))
        summary_path = output.with_name(output.stem + "_summary.json")
        summary_report = {key: value for key, value in report.items() if key != "samples"}
        summary_report["full_samples_path"] = str(output)
        summary_path.write_text(json.dumps(summary_report, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        print(f"完整 JSON 已保存：{output}")
        print(f"请用 Get-Content -Encoding UTF8 读取并回传摘要：{summary_path}")
        print("以上仅报告输入证据，不自动判定手柄、SDL 或映射故障。")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
