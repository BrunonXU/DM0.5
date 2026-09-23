"""Inspect without dependencies; preflight/shadow never enable command publishers."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
import uuid

from f1_replay.cli import BRUNO, runtime_lock
from f1_replay.runtime import ReplayError
from .config import DEFAULT_CONFIG, TASK, load_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="inspect",
                        choices=("inspect", "check-env", "capture-body", "preflight", "record-check", "shadow", "execute"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--endpoint", help="Override client HTTP endpoint, e.g. through an SSH forward")
    parser.add_argument("--chunks", type=int, default=1,
                        help="Fresh chunk attempts; expired observations are retried; 0 runs until interrupted")
    parser.add_argument("--output", type=Path, help="Parent directory for a new run directory")
    args = parser.parse_args(argv)
    try:
        doc, profile = load_config(args.config, require_body_reference=args.mode not in ("capture-body", "check-env"))
        if args.endpoint:
            doc["client"]["endpoint"] = args.endpoint
        if args.chunks < 0:
            raise ReplayError("chunks必须>=0")
        if args.mode == "inspect":
            result = {"contract": doc["contract"], "checkpoint": doc["checkpoint"],
                      "checkpoint_selected": bool(doc["checkpoint"]), "client": doc["client"],
                      "effective_profile": profile, "model_loaded": False,
                      "ros_opened": False, "hardware_published": False}
        elif args.mode == "check-env":
            from .ros import dependency_check
            result = dependency_check()
        elif args.mode == "capture-body":
            from .body_reference import capture
            with runtime_lock(BRUNO / "F1/foam_sort_whole_body/runtime"):
                result = capture(doc, profile, args.config)
        elif args.mode == "preflight":
            from .runtime import preflight
            result = preflight(doc, profile)
        else:
            from .runtime import run
            powered = args.mode == "execute"
            on_robot = socket.gethostname() == profile["expected_host"]
            parent = args.output or (BRUNO / "F1/foam_sort_whole_body/runtime/inference"
                                     if on_robot or powered else TASK / "results/inference")
            parent = parent.expanduser().resolve()
            if (on_robot or powered) and not parent.is_relative_to(BRUNO):
                raise ReplayError("F1运行输出必须在/home/user/Bruno内")
            output = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])
            output.mkdir(parents=True, exist_ok=False)
            if args.mode == 'record-check':
                from .record_probe import run as record_probe
                with runtime_lock(output):
                    result = record_probe(doc, profile, output)
            elif powered:
                with runtime_lock(output):
                    result = run(doc, profile, output, powered=True, chunks=args.chunks)
            else:
                result = run(doc, profile, output, chunks=args.chunks)
            result["output"] = str(output)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 1 if result.get("error") or result.get("ok") is False else 0
    except (ValueError, ReplayError, KeyError, OSError) as exc:
        print(f"F1推理：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
