from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace

import numpy as np
import torch
from PIL import Image

from opendm.constants.robot import RobotStateDesc
from opendm.data.augmentations import PadToSquare, TrainingTransformPipeline
from opendm.data.transforms import ActionAbsolute, ActionRelative
from opendm.dataset.NewLora_g2_sort import NEWLORA_STATE_DESC
from opendm.exp.dm05_exp import DM05InferenceConfig as BaseDM05InferenceConfig
from g2_sort.training.NewLora_dm05_lora import DM05InferenceConfig
from g2_sort.validation.newlora_common import mixed_to_absolute


def test_newlora_state_description_is_14_arm_plus_20_hand() -> None:
    assert len(NEWLORA_STATE_DESC) == 34
    assert NEWLORA_STATE_DESC[:14] == [RobotStateDesc.JOINT] * 14
    assert NEWLORA_STATE_DESC[14:] == [RobotStateDesc.GRIPPER] * 20


def test_newlora_mixed_relative_round_trip() -> None:
    state = np.linspace(-0.8, 0.8, 34, dtype=np.float32)
    action = np.stack([state + 0.1, state - 0.2], axis=0)
    action[:, 14:] = np.linspace(0.0, 1.0, 20, dtype=np.float32)
    data = {
        "state": state,
        "action": action.copy(),
        "meta_data": {"state_desc": NEWLORA_STATE_DESC},
    }

    relative = ActionRelative()(data)
    np.testing.assert_allclose(relative["action"][:, :14], action[:, :14] - state[:14])
    np.testing.assert_allclose(relative["action"][:, 14:], action[:, 14:])

    restored = ActionAbsolute()(relative)
    np.testing.assert_allclose(restored["action"], action, rtol=0.0, atol=1e-7)


def test_parallel_uploaded_image_decode_preserves_rgb_pixels() -> None:
    arrays = [
        np.random.default_rng(seed).integers(
            0, 256, size=(32, 48, 3), dtype=np.uint8
        )
        for seed in range(3)
    ]
    uploads = []
    expected = []
    for array in arrays:
        encoded = BytesIO()
        Image.fromarray(array, mode="RGB").save(encoded, format="JPEG", quality=95)
        payload = encoded.getvalue()
        uploads.append(BytesIO(payload))
        with Image.open(BytesIO(payload)) as image:
            expected.append(np.asarray(image.convert("RGB")))

    with ThreadPoolExecutor(max_workers=3) as executor:
        decoded = list(
            executor.map(DM05InferenceConfig._decode_uploaded_rgb, uploads)
        )

    for actual, reference in zip(decoded, expected, strict=True):
        np.testing.assert_array_equal(np.asarray(actual), reference)


def test_rtc_prefix_uses_physical_state_not_normalized_model_state() -> None:
    config = object.__new__(DM05InferenceConfig)
    config._rtc_action_relative = ActionRelative()
    config._rtc_action_normalize = lambda sample: sample
    config.rtc_max_guidance_weight = 0.5
    config.rtc_guidance_mode = "approx_identity"
    config.device = torch.device("cpu")
    config.model = SimpleNamespace(
        model=SimpleNamespace(
            action_in_proj=SimpleNamespace(weight=torch.zeros(1, dtype=torch.float32))
        )
    )

    physical_state = np.linspace(-0.7, 0.9, 34, dtype=np.float32)
    normalized_state = np.linspace(0.8, -0.8, 34, dtype=np.float32)
    absolute_prefix = np.stack(
        [physical_state + 0.1, physical_state - 0.2], axis=0
    )
    absolute_prefix[:, 14:] = np.linspace(0.0, 1.0, 20, dtype=np.float32)
    data = {
        # This deliberately represents the state after input_transform.
        "state": normalized_state,
        "meta_data": {"state_desc": NEWLORA_STATE_DESC},
        "rtc": {"absolute_prefix": absolute_prefix, "inference_delay": 1},
    }

    kwargs = config._prepare_rtc_model_kwargs(
        data,
        absolute_state=physical_state,
    )
    prefix = kwargs["rtc_prefix_actions"].cpu().numpy()[0]

    np.testing.assert_allclose(
        prefix[:, :14],
        absolute_prefix[:, :14] - physical_state[:14],
        rtol=0.0,
        atol=1e-7,
    )
    np.testing.assert_allclose(
        prefix[:, 14:], absolute_prefix[:, 14:], rtol=0.0, atol=1e-7
    )
    assert not np.allclose(
        prefix[:, :14], absolute_prefix[:, :14] - normalized_state[:14]
    )


def test_predict_passes_pre_transform_absolute_state_to_rtc_hook() -> None:
    class ProbeInferenceConfig(BaseDM05InferenceConfig):
        def _prepare_rtc_model_kwargs(self, data, *, absolute_state):
            self.probed_absolute_state = absolute_state.copy()
            self.probed_transformed_state = np.asarray(data["state"]).copy()
            return {}

    class FakeModel:
        def __init__(self):
            self.model = SimpleNamespace(
                config=SimpleNamespace(action_dim=34),
                action_in_proj=SimpleNamespace(
                    weight=torch.zeros(1, dtype=torch.float32)
                ),
            )

        def inference_action(self, **_kwargs):
            return torch.zeros(1, 50, 34, dtype=torch.float32)

    config = object.__new__(ProbeInferenceConfig)
    physical_state = np.linspace(-0.6, 0.8, 34, dtype=np.float32)
    normalized_state = np.linspace(0.9, -0.9, 34, dtype=np.float32)

    def mutate_input(sample):
        sample["state"] = normalized_state.copy()
        token = torch.zeros(1, 1, dtype=torch.int64)
        return {
            "input_ids": token,
            "attention_mask": token,
            "pixel_values": torch.zeros(1, 1),
            "token_type_ids": token,
        }

    config.input_transform = mutate_input
    config.output_transform = lambda sample: sample
    config.model = FakeModel()
    config.device = torch.device("cpu")
    config.output_action_dim = 34
    config.diffusion_steps = 1

    config._predict(
        {
            "state": physical_state.copy(),
            "meta_data": {"state_desc": NEWLORA_STATE_DESC},
            "rtc": None,
        }
    )

    np.testing.assert_array_equal(config.probed_absolute_state, physical_state)
    np.testing.assert_array_equal(config.probed_transformed_state, normalized_state)


def test_vendor_recommended_full_augmentation_contract() -> None:
    steps = TrainingTransformPipeline(p=0.5).steps
    assert len(steps) == 5
    assert isinstance(steps[0], PadToSquare)
    assert [type(step).__name__ for step in steps[1:]] == [
        "Resize",
        "RandomResizedCrop",
        "Rotate",
        "ColorJitter",
    ]
    assert [step.p for step in steps[2:]] == [0.5, 0.5, 0.5]


def test_validation_mixed_to_absolute_is_not_cumulative() -> None:
    state = np.arange(2 * 34, dtype=np.float32).reshape(2, 34) / 10.0
    mixed = np.ones((2, 50, 34), dtype=np.float32)
    mixed[..., 14:] = 0.25

    absolute = mixed_to_absolute(mixed, state)

    np.testing.assert_allclose(
        absolute[..., :14],
        np.broadcast_to(state[:, None, :14] + 1.0, (2, 50, 14)),
    )
    np.testing.assert_allclose(absolute[..., 14:], 0.25)
    np.testing.assert_allclose(absolute[:, 0], absolute[:, 49])
