import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image
import pytest

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "src"))
from f1_toys.processing import ACTIVE, Processor, crop_box, load_contract, model_actions, process_image, restore_actions


@pytest.fixture
def cfg():
    return load_contract(TASK / "configs/roi_right_v1.json")


def test_roi_includes_expected_bounds_and_preserves_aspect(cfg):
    assert crop_box((1280, 720), cfg) == (307, 28, 1229, 720)
    image = Image.new("RGB", (1280, 720), (255, 0, 0))
    result = process_image(image, "head", cfg)
    assert result.shape == (224, 224, 3)
    assert not result[0].any() and (result[100, :, 0] == 255).all()
    with pytest.raises(ValueError, match="resolution"):
        process_image(Image.new("RGB", (1920, 1080)), "head", cfg)


def test_three_cameras_and_same_processing(cfg):
    class Tokenizer:
        def tokenize(self, prompt, state):
            assert len(state) == 32
            return np.zeros(200, np.int64), np.ones(200, bool)
    stats = dict(mean=[0]*16, std=[1]*16)
    processor = Processor(cfg, {"state": stats}, Tokenizer())
    images = {c: Image.new("RGB", (1280, 720) if c == "head" else (640, 480), (12, 50, 80)) for c in cfg["cameras"]}
    train = processor.observation(images, np.arange(16))
    infer = processor.observation(images, np.arange(16))
    assert all(train["image_mask"].values())
    for k in train["image"]:
        np.testing.assert_array_equal(train["image"][k], infer["image"][k])
    with pytest.raises(ValueError, match="three"):
        processor.observation({"head": images["head"]}, np.zeros(16))


def test_right_action_roundtrip_and_left_controller_hold():
    stats = dict(mean=list(range(16)), std=[2]*16)
    actions = np.arange(50*16, dtype=np.float32).reshape(50, 16)
    model = model_actions(actions, stats)
    inactive = [i for i in range(32) if i not in ACTIVE]
    assert not model[:, inactive].any()
    model[:, inactive] = np.nan  # Untrained predictions are never adopted.
    held = np.arange(16, dtype=np.float32) + 80
    result = restore_actions(model, stats, held)
    np.testing.assert_allclose(result[:, list(ACTIVE)], actions[:, list(ACTIVE)])
    for dim in [0, 1, 2, 3, 4, 5, 6, 14]:
        assert (result[:, dim] == held[dim]).all()
    with pytest.raises(ValueError):
        restore_actions(model, stats, np.full(16, np.nan))


def test_empty_action_mask_rejected(tmp_path, cfg):
    cfg["active_action_indices"] = []
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError):
        load_contract(path)
