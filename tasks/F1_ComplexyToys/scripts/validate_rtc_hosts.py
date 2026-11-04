"""Same pixels/state/prefix/seed comparison across deployment runtimes, no robot."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from PIL import Image
from f1_toys.inference import Policy
from f1_toys.eef import physical_actions

p = argparse.ArgumentParser()
for name in ("bundle", "core", "inputs", "output"):
    p.add_argument("--" + name, type=Path, required=True)
p.add_argument("--compat", type=Path)
p.add_argument("--contexts", type=Path)
p.add_argument("--noise", type=Path)
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=False)
if a.compat:
    exec(compile(a.compat.read_text().split("from f1_toys.server import main")[0],
                 str(a.compat), "exec"), {"__file__": str(a.compat)})
from f1_toys.rtc_optimization import compile_suffix
policy = Policy(a.bundle, a.core)
compile_suffix(policy)
if a.noise:
    import torch
    from f1_toys import rtc
    noises = json.loads(a.noise.read_text())
    sample = rtc.sample_actions

    def shared_noise(model, observation, preprocess, **kwargs):
        seed = str(kwargs["rng"].initial_seed())
        kwargs["noise"] = torch.tensor(noises[seed], dtype=torch.float32,
                                      device=observation.state.device)
        return sample(model, observation, preprocess, **kwargs)

    rtc.sample_actions = shared_noise
observations = json.loads((a.inputs / "observations.json").read_text())
pairs = [r for r in json.loads((a.inputs / "pairs.json").read_text()) if r["shift"] == 20][:4]


def images_state(oid):
    row = observations[str(oid)]
    images = {}
    for camera, name in row["images"].items():
        path = a.inputs / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["image_sha256"][camera]
        images[camera] = Image.open(path).convert("RGB")
    return images, row["state16"]


if a.contexts:
    contexts = json.loads(a.contexts.read_text())
else:
    contexts = []
    for pair in pairs:
        images, state = images_state(pair["a"])
        for seed in (20261019, 20261020):
            ctx = dict(bootstrap=True, delay_steps=20, shift_steps=20, coverage_steps=42,
                       plan_origin_ns=0, base_plan_id=0,
                       committed_actions16=[], previous_absolute_eef10=[])
            result, _, _ = policy.predict_rtc(images, state, pair["held"], ctx, seed=seed)
            assert result["success"]
            contexts.append(dict(oid=pair["a"], held=pair["held"], seed=seed, ctx=dict(ctx)))
            ctx.update(bootstrap=False, base_plan_id=1,
                       committed_actions16=result["actions"][20:40],
                       previous_absolute_eef10=result["absolute_eef10"][20:])
            contexts.append(dict(oid=pair["b"], held=pair["held"], seed=seed, ctx=ctx))
(a.output / "contexts.json").write_text(json.dumps(contexts, indent=2))
results = []
for entry in contexts:
    images, state = images_state(entry["oid"])
    result, raw, _ = policy.predict_rtc(images, state, entry["held"], entry["ctx"], seed=entry["seed"])
    if result["success"] and not entry["ctx"]["bootstrap"]:
        assert np.array_equal(result["actions"][:20], entry["ctx"]["committed_actions16"])
    results.append(dict(success=result["success"], raw=raw.tolist(),
                        physical=physical_actions(raw, policy.stats["action"]).tolist(),
                        actions=result["actions"], model=policy.bundle_id,
                        timing_ms=result["timing_ms"]))
(a.output / "outputs.json").write_text(json.dumps(results))
print(json.dumps(dict(comparisons=len(results), success=sum(r["success"] for r in results),
                      contexts_sha256=hashlib.sha256((a.output/"contexts.json").read_bytes()).hexdigest())), flush=True)
