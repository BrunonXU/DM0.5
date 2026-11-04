#!/usr/bin/env python3
"""Package intact published-action windows as a portable smoke-only dataset."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys

import numpy as np
from PIL import Image

TASK = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(TASK / "src"), str(TASK / "DataPrehandle")]
from f1_toys.processing import load_contract, sha256
from build_training_index import Config, build_index


def statistics(values):
    array = np.asarray(values, dtype=np.float64)
    std = array.std(axis=0)
    constant = std < 1e-6
    return dict(mean=array.mean(axis=0).tolist(), std=np.where(constant, 1.0, std).tolist(),
                constant_dimensions=np.flatnonzero(constant).tolist(), count=len(array))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=TASK / "configs/roi_right_v1.json")
    args = parser.parse_args()
    cfg = load_contract(args.contract)
    raw, output = args.raw.resolve(), args.output.resolve()
    partial = output.with_name(output.name + ".partial")
    if output.exists() or partial.exists() or raw == output or raw in output.parents:
        raise ValueError("Use a new output directory outside raw data")
    partial.mkdir(parents=True)
    index_report = build_index(raw, partial / "index", mode="technical",
        config=Config(fps=cfg["fps"], horizon=cfg["horizon"], required_cameras=tuple(cfg["cameras"])),
        config_document={"source": "existing 30Hz whole-observation baseline; all three cameras required"})
    rows = [json.loads(line) for line in (partial / "index/anchors.jsonl").read_text().splitlines()]
    if not rows:
        raise ValueError("No complete valid action windows")
    source_manifest = json.loads((raw / "journal/manifest.json").read_text())
    states, actions, samples = [], [], []
    unique_states, unique_actions, images = {}, {}, {}
    for row in rows:
        paths = {}
        for frame in row["frames"]:
            camera = frame["camera"]
            relative = f"images/{camera}/{frame['payload_sha256']}.jpg"
            paths[camera] = relative
            if relative in images:
                continue
            source = (raw / "payload" / frame["path"]).resolve()
            if (raw / "payload").resolve() not in source.parents:
                raise ValueError("Image source escapes raw payload")
            with source.open("rb") as stream:
                stream.seek(frame["payload_offset"])
                data = stream.read(frame["payload_bytes"])
            if hashlib.sha256(data).hexdigest() != frame["payload_sha256"]:
                raise ValueError("Image source checksum failed")
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                expected = tuple(cfg["head_resolution"]) if camera == "head" else (frame["width"], frame["height"])
                if image.size != expected or image.mode != "RGB":
                    raise ValueError("Unexpected decoded image dimensions or color mode")
            destination = partial / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            images[relative] = frame["payload_sha256"]
        state = row["state"]
        action = [entry["action"] for entry in row["action_window"]]
        states.append(state)
        actions.append(action)
        unique_states[row["observation_id"]] = state
        for entry in row["action_window"]:
            unique_actions[entry["grid_monotonic_ns"]] = entry["action"]
        samples.append(dict(episode_id=row["episode_id"], observation_id=row["observation_id"],
                            anchor_monotonic_ns=row["anchor_monotonic_ns"], images=paths,
                            action_grid_ns=[a["grid_monotonic_ns"] for a in row["action_window"]],
                            action_control_sequences=[a["control_stream_sequence"] for a in row["action_window"]]))
    np.savez_compressed(partial / "arrays.npz", states=np.asarray(states, dtype=np.float32), actions=np.asarray(actions, dtype=np.float32))
    stats = {"state": statistics(list(unique_states.values())), "action": statistics(list(unique_actions.values())),
             "scope": "single source episode; pipeline smoke only; recompute on formal training split"}
    (partial / "norm_stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    shutil.copyfile(args.contract, partial / "contract.json")
    (partial / "samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in samples))
    files = {str(p.relative_to(partial)): sha256(p) for p in sorted(partial.rglob("*")) if p.is_file()}
    report = dict(schema="f1_toys_dataset_v1", smoke_only=True, formal_training_ready=False,
        user_authorization="Use the V1-reset-fixed folder sample to validate training/smoke pipeline",
        human_success_reviewed=False, source_episode_id=index_report["source_episode_id"],
        source_manifest_sha256=index_report["source_manifest_sha256"],
        source_binding=source_manifest["binding"], user_folder="V1复位修复后",
        source_format="f1 raw journal/payload; direct audited anchor adapter, not a LeRobot downgrade",
        split="smoke-only; no independent validation episode",
        index_summary=index_report["summary"], samples=len(samples), image_files=len(images),
        contract_sha256=sha256(partial / "contract.json"), files_sha256=files)
    (partial / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    os.replace(partial, output)
    print(json.dumps({"output": str(output), "samples": len(samples), "image_files": len(images), "summary": index_report["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
