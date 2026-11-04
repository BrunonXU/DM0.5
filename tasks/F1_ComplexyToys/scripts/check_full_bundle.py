#!/usr/bin/env python3
"""Verify exact Full export and actual recorded-input sampling; no robot or IK."""
import argparse
import json
from pathlib import Path
import sys
import numpy as np
import torch
from safetensors import safe_open

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.dataset import F1Dataset
from f1_toys.inference import Policy, batched_observation
from f1_toys.processing import active_cameras, process_image


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for k in ("checkpoint", "bundle", "dataset", "rlinf-root", "output"):
        p.add_argument("--" + k, type=Path, required=True)
    a = p.parse_args()
    state = torch.load(a.checkpoint / "model_state_dict/full_weights.pt",
                       weights_only=True, mmap=True, map_location="cpu")
    with safe_open(str(a.bundle / "model.safetensors"), framework="pt") as f:
        assert set(state) == {"model." + k for k in f.keys()}
        for k in f.keys():
            assert torch.equal(state["model." + k], f.get_tensor(k)), k
    del state
    policy = Policy(a.bundle, a.rlinf_root)
    ds = F1Dataset(a.dataset)
    assert policy.contract == ds.contract and policy.stats == ds.stats
    indices = [0, next(i for i, r in enumerate(ds.rows) if r["supervision_phase"] == "home"),
               next(i for i, r in enumerate(ds.rows) if r["valid_action_steps"] < 50)]
    results = []
    isolation = None
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
        for i in indices:
            data = policy.processor.observation(ds.raw_images(i), ds.states[i])
            train = ds[i][0]
            for k in data:
                if isinstance(data[k], dict):
                    for key in data[k]:
                        np.testing.assert_array_equal(data[k][key], train[k][key])
                else:
                    np.testing.assert_array_equal(data[k], train[k])
            obs = batched_observation(data, policy.observation_type, "cuda")
            pred = policy.model.sample_actions(obs, num_steps=5,
                        rng=torch.Generator(device="cuda").manual_seed(20261020))
            assert pred.shape == (1, 50, 32) and torch.isfinite(pred).all()
            assert not pred[..., 10:].any()
            results.append(dict(index=i, phase=ds.rows[i]["supervision_phase"],
                                valid_steps=ds.rows[i]["valid_action_steps"], finite=True))
        if ds.contract.get('input_version') == 'right_only_v1':
            i = indices[0]
            images, state = ds.raw_images(i), ds.states[i].copy()
            reference = policy.processor.observation(images,state)
            changed = state.copy();changed[:7]=[30,-40,20,-80,10,5,-15];changed[14]=100
            from PIL import Image
            images['left_wrist']=Image.new('RGB',(640,480),(255,0,255))
            perturbed = policy.processor.observation(images,changed)
            pixels = {c:process_image(images[c],c,ds.contract) for c in active_cameras(ds.contract)}
            prepared = policy.observation_from_pixels(pixels,changed)
            for other in (perturbed,prepared):
                for key in reference:
                    if isinstance(reference[key],dict):
                        for name in reference[key]:np.testing.assert_array_equal(reference[key][name],other[key][name])
                    else:np.testing.assert_array_equal(reference[key],other[key])
            predictions=[]
            for data in (reference,perturbed,prepared):
                obs=batched_observation(data,policy.observation_type,'cuda')
                predictions.append(policy.model.sample_actions(obs,num_steps=5,
                    rng=torch.Generator(device='cuda').manual_seed(713)))
            for out in predictions[1:]:torch.testing.assert_close(out,predictions[0],rtol=0,atol=0)
            obs=batched_observation(reference,policy.observation_type,'cuda')
            _, prefix_mask, _=policy.model.embed_prefix(obs)
            # Each 224px SigLIP image produces 256 tokens, then prompt tokens.
            assert prefix_mask[0,:256].all() and not prefix_mask[0,256:512].any() and prefix_mask[0,512:768].all()
            isolation=dict(input_version='right_only_v1',left_state_and_image_perturbation='exactly_equal',
                prepared_path='exactly_equal',same_noise_model_prediction='exactly_equal',left_image_attention_mask=False)
    if isolation is not None:
        # Use the deployed suffix compiler and actual RTC VJP path. Ordinary
        # tensors are required here: inference tensors cannot enter autograd.
        from f1_toys.rtc import sample_actions as rtc_sample
        from f1_toys.rtc_optimization import compile_suffix
        compile_suffix(policy)
        compiled_checks = []
        with torch.autocast('cuda', dtype=torch.bfloat16):
            for guided in (False, True):
                outputs = []
                for data in (reference, perturbed, prepared):
                    obs = batched_observation(data, policy.observation_type, 'cuda')
                    out = rtc_sample(policy.model, obs, policy.preprocess_observation,
                        target=np.zeros((1,50,32),np.float32) if guided else None,
                        delay=14, shift=16, steps=5,
                        rng=torch.Generator(device='cuda').manual_seed(713))
                    assert torch.isfinite(out).all() and not out[...,10:].any()
                    outputs.append(out)
                for out in outputs[1:]:
                    torch.testing.assert_close(out,outputs[0],rtol=0,atol=0)
                compiled_checks.append('guided_vjp' if guided else 'bootstrap')
        isolation['compiled_suffix_same_noise_prediction'] = dict(
            result='exactly_equal', paths=compiled_checks)
    result = dict(result="PASS", full_export_exact=True, inputs_equal=True,
                  samples=results, input_isolation=isolation, robot_published=False, task_success_evaluated=False)
    a.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
