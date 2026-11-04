#!/usr/bin/env python3
"""Classify only the eight known F1 runs; preserve history and existing tags."""
import argparse
import json
from pathlib import Path
import wandb

PLAN = {
    "f1eefsinglepre20": ("00_Engineering_Checks", "preflight", "single-demo"),
    "f1eefresume751v1": ("00_Engineering_Checks", "resume-check", "single-demo"),
    "f1eeffourpre20": ("00_Engineering_Checks", "preflight", "single-demo"),
    "f1eefresume751fix": ("00_Engineering_Checks", "resume-check", "single-demo"),
    "f1v1formal20261016pre1": ("00_Engineering_Checks", "preflight", "V1-15demos"),
    "f1v1formal20261016pre2": ("00_Engineering_Checks", "preflight", "V1-15demos"),
    "f1eef20261016": ("01_SingleDemo_LoRA", "train", "single-demo"),
    "f1v1formal20261016main1": ("02_V1_LoRA", "train", "V1-15demos"),
}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=False)
    api = wandb.Api()
    runs = {key: api.run("robotics-example/f1_complexytoys/" + key) for key in PLAN}
    def record(r):
        return dict(id=r.id, name=r.name, group=r.group, job_type=r.job_type,
                    tags=r.tags, state=r.state, last_step=r.summary.get("_step"))
    before = [record(r) for r in runs.values()]
    (a.output / "before.json").write_text(json.dumps(before, indent=2) + "\n")
    for key, r in runs.items():
        group, job, dataset = PLAN[key]
        r.group, r.job_type = group, job
        r.tags = sorted(set(r.tags or []) | {"F1", "LoRA", dataset, job})
        r.update()
    api.flush()
    after = [record(api.run("robotics-example/f1_complexytoys/" + key)) for key in PLAN]
    for old, new in zip(before, after):
        assert old["last_step"] == new["last_step"] and old["name"] == new["name"]
        assert new["group"] == PLAN[new["id"]][0] and new["job_type"] == PLAN[new["id"]][1]
    (a.output / "after.json").write_text(json.dumps(after, indent=2) + "\n")
    print(json.dumps(after), flush=True)


if __name__ == "__main__":
    main()
