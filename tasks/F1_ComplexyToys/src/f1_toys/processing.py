"""Deterministic RGB/ROI and numerical transforms shared by data and inference."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image

CAMERA_KEYS = {"head": "base_0_rgb", "left_wrist": "left_wrist_0_rgb", "right_wrist": "right_wrist_0_rgb"}
ACTIVE = (7, 8, 9, 10, 11, 12, 13, 15)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_contract(path: Path) -> dict:
    cfg = json.loads(Path(path).read_text())
    eef = cfg["schema"] == "f1_toys_eef_contract_v2"
    if (cfg["schema"] not in ("f1_toys_contract_v1", "f1_toys_eef_contract_v2") or cfg["contract"] != "f1_upper_16d_v1"
            or tuple(cfg["active_action_indices"]) != (tuple(range(10)) if eef else ACTIVE) or cfg["model_action_dim"] != 32
            or cfg["state_dim"] != 16 or cfg["cameras"] != list(CAMERA_KEYS)):
        raise ValueError("Unsupported F1 task contract")
    x0, y0, x1, y1 = cfg["head_roi_xyxy_normalized"]
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError("ROI must lie inside the source image")
    if (cfg["train_image_augmentation"] and not eef) or not cfg["mask_inactive_action_inputs"]:
        raise ValueError("v1 requires identical image geometry and inactive latent masking")
    if "input_version" in cfg:
        if (not eef or cfg["input_version"] != "right_only_v1"
                or cfg.get("active_state_indices") != list(ACTIVE)
                or cfg.get("active_cameras") != ["head", "right_wrist"]):
            raise ValueError("Unsupported versioned model input isolation")
    elif "active_state_indices" in cfg or "active_cameras" in cfg:
        raise ValueError("Input masks require an explicit version")
    return cfg


def active_cameras(contract):
    return tuple(contract.get("active_cameras", contract["cameras"]))


def crop_box(size: tuple[int, int], cfg: dict) -> tuple[int, int, int, int]:
    if list(size) != cfg["head_resolution"]:
        raise ValueError(f"Head resolution changed: {size}; revalidate ROI contract")
    x0, y0, x1, y1 = cfg["head_roi_xyxy_normalized"]
    width, height = size
    return math.floor(x0 * width), math.floor(y0 * height), math.ceil(x1 * width), math.ceil(y1 * height)


def process_image(image: Image.Image, camera: str, cfg: dict) -> np.ndarray:
    if camera not in CAMERA_KEYS:
        raise ValueError(f"Unknown camera {camera}")
    if image.mode != "RGB":
        image = image.convert("RGB")
    if camera == "head":
        image = image.crop(crop_box(image.size, cfg))
    target = int(cfg["image_size"])
    ratio = min(target / image.width, target / image.height)
    width, height = max(1, round(image.width * ratio)), max(1, round(image.height * ratio))
    resized = image.resize((width, height), Image.Resampling.BILINEAR)
    output = Image.new("RGB", (target, target), (0, 0, 0))
    output.paste(resized, ((target - width) // 2, (target - height) // 2))
    return np.asarray(output, dtype=np.uint8).copy()


def normalize(values: np.ndarray, stats: dict) -> np.ndarray:
    values = np.asarray(values, dtype=np.float32)
    mean, scale = np.asarray(stats["mean"], np.float32), np.asarray(stats["std"], np.float32)
    if values.shape[-1] != 16 or mean.shape != (16,) or scale.shape != (16,):
        raise ValueError("Expected 16D state/action and matching statistics")
    if not np.isfinite(values).all() or not np.isfinite(mean).all() or not np.isfinite(scale).all() or (scale <= 0).any():
        raise ValueError("Non-finite input or invalid normalization scale")
    return (values - mean) / scale


def model_actions(actions16: np.ndarray, stats: dict) -> np.ndarray:
    result = np.zeros((*actions16.shape[:-1], 32), dtype=np.float32)
    normalized = normalize(actions16, stats)
    result[..., list(ACTIVE)] = normalized[..., list(ACTIVE)]
    return result


def restore_actions(actions32: np.ndarray, stats: dict, last_submitted16: np.ndarray) -> np.ndarray:
    """Only learned right-side coordinates may reach the external 16D contract."""
    actions = np.asarray(actions32, dtype=np.float32)
    held = np.asarray(last_submitted16, dtype=np.float32)
    if actions.ndim != 2 or actions.shape[1] != 32 or held.shape != (16,):
        raise ValueError("Expected Hx32 predictions and controller last_submitted16")
    if not np.isfinite(actions[..., list(ACTIVE)]).all() or not np.isfinite(held).all():
        raise ValueError("Non-finite predicted right action or controller hold target")
    output = np.broadcast_to(held, (len(actions), 16)).copy()
    mean, std = np.asarray(stats["mean"], np.float32), np.asarray(stats["std"], np.float32)
    output[:, list(ACTIVE)] = actions[:, list(ACTIVE)] * std[list(ACTIVE)] + mean[list(ACTIVE)]
    if not np.isfinite(output).all():
        raise ValueError("Non-finite denormalized action")
    return output


class Processor:
    def __init__(self, contract: dict, stats: dict, tokenizer):
        self.contract, self.stats, self.tokenizer = contract, stats, tokenizer

    def model_state(self, state16):
        """Single owner of normalization AND isolation, before state tokenization.

        Raw physical state remains available to the caller for FK and safety.
        Diagnostics describe physical inputs, never become model conditions.
        """
        if self.contract["schema"] == "f1_toys_eef_contract_v2":
            from .eef import normalize_state
            normalized, self.last_state_diagnostics = normalize_state(state16, self.stats["state"])
        else:
            normalized = normalize(state16, self.stats["state"])
        state = np.zeros(32, dtype=np.float32)
        indices = self.contract.get("active_state_indices", list(range(16)))
        state[indices] = normalized[indices]
        return state

    def observation(self, images: dict[str, Image.Image], state16: np.ndarray, *, augmentation_seed=None,
                    prepared_pixels=False) -> dict:
        active = active_cameras(self.contract)
        if not set(active).issubset(images) or not set(images).issubset(CAMERA_KEYS):
            if self.contract.get("input_version"):
                raise ValueError("All active camera images are required")
            raise ValueError("All three valid camera images are required")
        state = self.model_state(state16)
        tokens, mask = self.tokenizer.tokenize(self.contract["prompt"], state)
        if prepared_pixels:
            if augmentation_seed is not None:
                raise ValueError("Prepared inference pixels cannot use training augmentation")
            pixels = {c: np.asarray(images[c]) for c in active}
            shape = (self.contract['image_size'], self.contract['image_size'], 3)
            if any(x.shape != shape or x.dtype != np.uint8 for x in pixels.values()):
                raise ValueError("Prepared pixels must be exact RGB uint8 224")
        else:
            pixels = {c: process_image(images[c], c, self.contract) for c in active}
        if augmentation_seed is not None:
            if not self.contract["train_image_augmentation"]:
                raise ValueError("Augmentation disabled in contract")
            from .augmentation import augment
            pixels = {c: augment(pixels[c], c, augmentation_seed) for c in active}
        for c in CAMERA_KEYS:
            if c not in active:
                pixels[c] = np.zeros((self.contract['image_size'], self.contract['image_size'], 3), np.uint8)
        return dict(
            image={CAMERA_KEYS[c]: pixels[c].astype(np.float32) / 127.5 - 1 for c in CAMERA_KEYS},
            image_mask={key: np.bool_(c in active) for c, key in CAMERA_KEYS.items()},
            state=state, tokenized_prompt=np.asarray(tokens, dtype=np.int64),
            tokenized_prompt_mask=np.asarray(mask, dtype=np.bool_),
        )
