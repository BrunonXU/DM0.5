"""Use the frozen F1 reader, shared DM0.5 generation and existing prefix IK."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import time
from types import SimpleNamespace

import numpy as np

from f1_data.reader import CAMERAS, Normalizer, PARTIAL_BASE_CONTRACT, PROMPT, require, sha256
from . import CONTRACT

TASK_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = TASK_ROOT / 'configs/inference.json'


@contextmanager
def request_seed(seed, device):
    """Optional paired sampling; preserve normal online RNG progression."""
    if seed is None:
        yield
        return
    require(type(seed) is int and 0 <= seed < 2**32, 'seed must be an integer in [0,2^32)')
    import torch
    target = torch.device(device)
    index = (target.index if target.index is not None else torch.cuda.current_device()) if target.type == 'cuda' else None
    with torch.random.fork_rng(devices=[] if index is None else [index]):
        torch.random.default_generator.manual_seed(seed)
        if index is not None:
            torch.cuda.default_generators[index].manual_seed(seed)
        yield


def load_config(path=DEFAULT_CONFIG):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    require(config.get('contract') == CONTRACT, 'F1 inference contract mismatch')
    require(config.get('policy_contract') == PARTIAL_BASE_CONTRACT, 'F1 policy contract mismatch')
    for key in ('release', 'checkpoint'):
        value = config.get(key)
        require(key == 'checkpoint' or isinstance(value, str) and bool(value), 'release path required')
        require(value is None or isinstance(value, str), f'{key} must be a path or null')
        config[key] = str((path.parent / value).resolve()) if value else None
    server = config.setdefault('server', {})
    require(server.get('diffusion_steps', 10) == 10, 'frozen F1 recipe requires 10 Euler steps')
    require(type(server.get('port', 7892)) is int and 0 < server.get('port', 7892) < 65536, 'invalid server port')
    require(type(server.get('max_request_bytes', 16777216)) is int and server.get('max_request_bytes', 16777216) > 0,
            'invalid max_request_bytes')
    return config


def load_binding(config):
    """Read release/stats only; serving does not scan images or fit statistics."""
    path = Path(config['release'])
    doc = json.loads(path.read_text())
    require(doc.get('schema') == 'f1_dm05_release_v1', 'release schema mismatch')
    require(doc.get('training_approved') is True, 'inference requires a frozen approved release')
    require(doc.get('contract') == config['policy_contract'], 'release/policy contract mismatch')
    from f1_training.data import verify_file
    stats_path = verify_file(doc['statistics'], path.parent)
    normalizer = Normalizer(json.loads(stats_path.read_text()))
    require(normalizer.stats.get('training_approved') is True, 'inference requires approved statistics')
    require(normalizer.stats['schema'] == doc['contract'], 'statistics/release contract mismatch')
    binding = {'release_sha256': sha256(path), 'statistics_sha256': sha256(stats_path),
               'policy_contract': doc['contract'], 'ik_version': 'f1_ikfk_demo_v1',
               'checkpoint_identity': None}
    if config.get('checkpoint'):
        from f1_training.validation_artifacts import validate_checkpoint
        identity = validate_checkpoint(config['checkpoint'], SimpleNamespace(path=path, stats_path=stats_path))
        require(identity['binding'].get('ik_version') == binding['ik_version'], 'checkpoint/IK version mismatch')
        binding['checkpoint_identity'] = identity
    return normalizer, binding


def observation_sample(state19, normalizer):
    """Reuse reader normalization exactly; dummy labels never reach the model."""
    state = np.asarray(state19, dtype=np.float32)
    require(state.shape == (19,) and np.isfinite(state).all(), 'finite physical state19 required')
    mask = np.zeros((50, 32), dtype=bool)
    mask[:, :23] = True
    sample = normalizer({'state': state, 'state_mask': np.ones(19, dtype=bool),
                         'action': np.zeros((50, 32), dtype=np.float32), 'action_mask': mask,
                         'prompt': PROMPT,
                         'meta_data': {'robot_type': 'F1', 'control_mode': 'EEF relative'}})
    return sample


class DM05Policy:
    """Only instantiated by explicit serve; never creates robot interfaces."""

    def __init__(self, config, normalizer, binding):
        require(config.get('checkpoint'), 'checkpoint is empty; select the foam checkpoint before serve')
        import torch
        from transformers import AutoProcessor
        from opendm.exp.dm05_exp import DM05ModelConfig
        from opendm.model.dm05.dm05_lora import unwrap_dm05_model
        from opendm.data.collator import TrainingCollator
        from f1_data.images import ImageLoader
        from f1_data.state_tokens import make_tokenizer
        from f1_training.experiment import read_recipe
        from f1_training.execution import ActionDecoder

        self.device = config.get('server', {}).get('device', 'cuda:0')
        require(str(self.device).startswith('cuda'), 'frozen BF16/FlashAttention recipe requires the existing CUDA environment')
        require(torch.cuda.is_available(), 'CUDA is unavailable; inspect works without loading the model')
        recipe = read_recipe()
        self.normalizer, self.binding = normalizer, binding
        model = DM05ModelConfig(model_name_or_path=config['checkpoint'], **recipe['model']).build_model(use_lora=False)
        self.model = unwrap_dm05_model(model.to(self.device).eval())
        self.model.enable_gradient_checkpointing(vlm_gradient_checkpointing=False, ae_gradient_checkpointing=False)
        processor = AutoProcessor.from_pretrained(binding['checkpoint_identity']['binding']['base_model'], local_files_only=True)
        self.tokenizer = make_tokenizer(processor, image_keys=['images_1', 'images_2', 'images_3'], max_length=1024)
        self.collator = TrainingCollator(processor.tokenizer.pad_token_id, max_length=1024)
        # Reuse exactly the validation view transforms, including resize/crop.
        loader = ImageLoader('.', training=False)
        loader._build_transforms()
        self.transforms = loader.transforms
        self.decoder = ActionDecoder(normalizer)

    def prepare_inputs(self, observation, images):
        """Same validation preprocessing, separately callable for CPU verification."""
        from PIL import Image
        from f1_training.evaluation import generation_inputs

        sample = observation_sample(observation['state19'], self.normalizer)
        require(set(images) == set(CAMERAS), 'head/left/right images required')
        for i, (camera, transform) in enumerate(zip(CAMERAS, self.transforms)):
            pixels = transform(image=np.asarray(images[camera].convert('RGB')))['image']
            sample[f'images_{i+1}'] = Image.fromarray(pixels).convert('RGB')
        inputs = self.tokenizer(sample)
        for key in ('action', 'action_mask'):
            inputs[key] = inputs[key].unsqueeze(0)
        batch = self.collator([inputs])
        return {k: v.to(self.device) for k, v in generation_inputs(batch).items()}

    def predict(self, observation, images):
        import torch

        started = time.monotonic()
        inputs = self.prepare_inputs(observation, images)
        prepared = time.monotonic()
        with request_seed(observation.get('seed'), self.device), torch.no_grad(), torch.autocast('cuda', dtype=torch.bfloat16):
            output = self.model.inference_action(**inputs, diffusion_steps=10)
        require(tuple(output.shape) == (1, 50, 32), 'expected native F1 H50/32D model output')
        normalized = output[0].float().cpu().numpy()
        generated = time.monotonic()
        physical = self.normalizer.denormalize_action(normalized)
        solved = self.decoder.decode(normalized, observation['state19'][:14], observation['reference_joints14_deg'])
        solved['inference_diagnostics'] = {
            'physical_action23': [[float(v) if np.isfinite(v) else None for v in row] for row in physical],
            'nonfinite_elements': int((~np.isfinite(physical)).sum()),
            'action_semantics': 'fixed_observation_eef_relative_m_rot6d_rows_grippers_0_100_base_m_s_rad_s',
            'timing_seconds': {'preprocess': prepared-started, 'model': generated-prepared,
                               'ik': time.monotonic()-generated}}
        return solved
