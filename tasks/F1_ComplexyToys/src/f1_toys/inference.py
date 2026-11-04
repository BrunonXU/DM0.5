"""Offline inference adapter. No ROS import, robot connection or publication."""
from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import torch

from .processing import Processor, load_contract, restore_actions, sha256
from .tokenizer import Tokenizer


def load_core(rlinf_root):
    """Load the unchanged core package without importing the Ray scheduler.

    All relative imports resolve to the same frozen RLinf source files used by
    training. This is an import namespace, not another model implementation.
    """
    directory = Path(rlinf_root).resolve() / "rlinf/models/embodiment/openpi_rlinf/pi0_model"
    name = "f1_toys_rlinf_core"
    if name in sys.modules:
        if Path(sys.modules[name].__file__).parent != directory:
            raise ValueError("Cannot mix two RLinf core versions in one process")
    else:
        spec = importlib.util.spec_from_file_location(name, directory / "__init__.py", submodule_search_locations=[str(directory)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(name + ".pi0_config"), importlib.import_module(name + ".model")


def batched_observation(data, observation_type, device):
    tensors = {}
    for key, value in data.items():
        if key == '_f1_metadata':
            continue  # Sampling diagnostics are never model conditions.
        if isinstance(value, dict):
            tensors[key] = {k: torch.as_tensor(v[None], device=device) for k, v in value.items()}
        else:
            tensors[key] = torch.as_tensor(value[None], device=device)
    return observation_type.from_dict(tensors)


class Policy:
    def __init__(self, bundle, rlinf_root, *, device="cuda", tokenizer=None):
        bundle = Path(bundle)
        manifest = json.loads((bundle / "bundle.json").read_text())
        if manifest["schema"] != "f1_toys_inference_bundle_v1":
            raise ValueError("Unsupported inference bundle")
        for name, digest in manifest["files_sha256"].items():
            if sha256(bundle / name) != digest:
                raise ValueError(f"Inference bundle integrity failure: {name}")
        core_dir = Path(rlinf_root) / "rlinf/models/embodiment/openpi_rlinf/pi0_model"
        for name, digest in manifest["core_sha256"].items():
            if sha256(core_dir / name) != digest:
                raise ValueError(f"Training/inference core mismatch: {name}")
        for name, digest in manifest["preprocessing_sha256"].items():
            if sha256(Path(__file__).parent / name) != digest:
                raise ValueError(f"Training/inference preprocessing mismatch: {name}")
        self.contract = load_contract(bundle / "contract.json")
        self.stats = json.loads((bundle / "norm_stats.json").read_text())
        tokenizer = Path(tokenizer) if tokenizer else bundle / "paligemma_tokenizer.model"
        if sha256(tokenizer) != manifest["files_sha256"]["paligemma_tokenizer.model"]:
            raise ValueError("Tokenizer differs from the training asset")
        self.processor = Processor(self.contract, self.stats, Tokenizer(self.contract["max_token_len"], tokenizer))
        config_module, model_module = load_core(rlinf_root)
        config = json.loads((bundle / "model_config.json").read_text())
        with torch.device("meta"):
            self.model = config_module.Pi0Config(**config).create()
        from safetensors.torch import load_file
        self.model.load_state_dict(load_file(str(bundle / "model.safetensors")), strict=True, assign=True)
        self.model = self.model.to(device=device, dtype=torch.float32).eval()
        self.observation_type, self.device = model_module.Observation, device
        self.preprocess_observation = model_module.preprocess_observation
        self.bundle_id = sha256(bundle / "bundle.json")
        self.input_preprocessing = dict(implementation_sha256=sha256(Path(__file__).parent/'processing.py'),
                                        contract_sha256=sha256(bundle/'contract.json'))

    @torch.inference_mode()
    def predict(self, images, state16, last_submitted16, *, steps=5, seed=20261015):
        data = self.processor.observation(images, np.asarray(state16, np.float32))
        obs = batched_observation(data, self.observation_type, self.device)
        generator = torch.Generator(device=self.device).manual_seed(seed)
        with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=str(self.device).startswith("cuda")):
            predicted = self.model.sample_actions(obs, num_steps=steps, rng=generator)
        actions = restore_actions(predicted[0].float().cpu().numpy(), self.stats["action"], last_submitted16)
        return actions, data

    @torch.inference_mode()
    def predict_eef(self, images, state16, held16, *, steps=10, seed=None, execute_steps=20):
        import time
        from .eef import physical_actions, decode_prefix, solver
        if self.contract["schema"] != "f1_toys_eef_contract_v2":
            raise ValueError("EEF service requires an EEF checkpoint")
        if not hasattr(self, "ik"):
            self.ik = solver()
        started = time.perf_counter()
        data = self.processor.observation(images, np.asarray(state16, float))
        processed = time.perf_counter()
        obs = batched_observation(data, self.observation_type, self.device)
        generator = None if seed is None else torch.Generator(device=self.device).manual_seed(seed)
        with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=str(self.device).startswith("cuda")):
            predicted = self.model.sample_actions(obs, num_steps=steps, rng=generator)
        raw = predicted[0].float().cpu().numpy()
        inferred = time.perf_counter()
        physical = physical_actions(raw, self.stats["action"])
        decoded = decode_prefix(self.ik, physical, state16, held16, execute_steps)
        finished = time.perf_counter()
        decoded.update(prediction_horizon=50, execute_steps=execute_steps,
                       timing_ms=dict(preprocess=(processed-started)*1000, model_h2d_d2h=(inferred-processed)*1000,
                                      ik=(finished-inferred)*1000, policy=(finished-started)*1000),
                       state_diagnostics=self.processor.last_state_diagnostics)
        return decoded, raw, data

    def predict_rtc(self, images, state16, held16, rtc, *, steps=5, seed=None, guided=True, prepared_pixels=False):
        import time
        from .eef import solver, physical_actions
        from .rtc import sample_actions, reanchor, absolute_eef, decode_window
        if self.contract['schema'] != 'f1_toys_eef_contract_v2':
            raise ValueError('RTC requires EEF v2 bundle')
        if not hasattr(self, 'ik'):
            self.ik = solver()
        started = time.perf_counter()
        if prepared_pixels:
            data = self.observation_from_pixels(images, state16)
        else:
            data = self.processor.observation(images, np.asarray(state16, float))
        obs = batched_observation(data, self.observation_type, self.device)
        committed = rtc['committed_actions16']
        target = None if rtc['bootstrap'] or not guided else reanchor(self.ik, state16,
            rtc['previous_absolute_eef10'], committed, self.stats['action'])
        prepared = time.perf_counter()
        generator = None if seed is None else torch.Generator(device=self.device).manual_seed(seed)
        with torch.autocast(device_type='cuda', dtype=torch.bfloat16, enabled=str(self.device).startswith('cuda')):
            predicted = sample_actions(self.model, obs, self.preprocess_observation,
                target=target, delay=rtc['delay_steps'], shift=rtc['shift_steps'],
                steps=steps, beta=5., rng=generator)
        raw = predicted[0].float().cpu().numpy()
        inferred = time.perf_counter()
        physical = physical_actions(raw, self.stats['action'])
        decoded = decode_window(self.ik, physical, state16, held16, committed, rtc['coverage_steps'])
        absolute = absolute_eef(self.ik, state16, raw, self.stats['action'])
        finished = time.perf_counter()
        decoded.update(absolute_eef10=absolute.tolist(),
            timing_ms=dict(preprocess=(prepared-started)*1000, model_h2d_d2h=(inferred-prepared)*1000,
                           ik=(finished-inferred)*1000, policy=(finished-started)*1000))
        return decoded, raw, data

    def observation_from_pixels(self, images, state16):
        """Same frozen state/token transforms; image geometry was verified on ARM."""
        return self.processor.observation(images, np.asarray(state16, float), prepared_pixels=True)
