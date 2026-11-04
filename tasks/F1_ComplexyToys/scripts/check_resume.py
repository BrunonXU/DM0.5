#!/usr/bin/env python3
"""Read only the saved data cursor and selected Adam step scalars from DCP."""
import argparse
import json
from pathlib import Path

import torch
from torch.distributed.checkpoint import FileSystemReader, load


def inspect(root):
    data = torch.load(root / "data.pt", weights_only=False)
    reader = FileSystemReader(str(root / "dcp_checkpoint"))
    metadata = reader.read_metadata()
    keys = [k for k in metadata.state_dict_metadata if k.endswith(".step") and "action_out_proj" in k]
    state = {}
    for key in keys:
        cursor = state
        for item in metadata.planner_data[key][:-1]:
            cursor = cursor.setdefault(item, {})
        cursor[metadata.planner_data[key][-1]] = torch.tensor(0., dtype=metadata.state_dict_metadata[key].properties.dtype)
    load(state, storage_reader=reader)
    steps = {}
    for key in keys:
        cursor = state
        for item in metadata.planner_data[key]:
            cursor = cursor[item]
        steps[key] = float(cursor.item())
    return dict(checkpoint=str(root), data=data, optimizer_steps=steps)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--additional-steps", type=int, default=1)
    parser.add_argument("--accumulation", type=int, default=1)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    before, after = inspect(args.before), inspect(args.after)
    assert before["optimizer_steps"] and before["optimizer_steps"].keys() == after["optimizer_steps"].keys()
    for name, step in before["optimizer_steps"].items():
        assert after["optimizer_steps"][name] == step + args.additional_steps
    # Preflight stays inside one epoch; each optimizer step consumes accumulation batches.
    for first, second in zip(before["data"], after["data"], strict=True):
        assert second["_num_yielded"] == first["_num_yielded"] + args.additional_steps * args.accumulation
    report = dict(result="PASS", checkpoints=[before, after])
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
