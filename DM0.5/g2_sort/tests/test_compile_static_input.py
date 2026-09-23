from __future__ import annotations

import pytest
import torch

from opendm.data.transforms import PadModelInput


def _model_input(length: int) -> dict[str, torch.Tensor]:
    return {
        "input_ids": torch.arange(length, dtype=torch.long).unsqueeze(0),
        "attention_mask": torch.ones(1, length, dtype=torch.long),
        "token_type_ids": torch.full((1, length), 7, dtype=torch.long),
        "pixel_values": torch.randn(1, 3, 4, 4),
    }


def test_compile_padding_preserves_valid_tokens_and_pixels() -> None:
    original = _model_input(5)
    pixels = original["pixel_values"].clone()

    padded = PadModelInput(max_length=8, pad_token_id=99)(original)

    torch.testing.assert_close(padded["input_ids"][:, :5], torch.arange(5)[None])
    torch.testing.assert_close(
        padded["attention_mask"][:, :5], torch.ones(1, 5, dtype=torch.long)
    )
    torch.testing.assert_close(
        padded["token_type_ids"][:, :5], torch.full((1, 5), 7, dtype=torch.long)
    )
    torch.testing.assert_close(padded["input_ids"][:, 5:], torch.full((1, 3), 99))
    torch.testing.assert_close(
        padded["attention_mask"][:, 5:], torch.zeros(1, 3, dtype=torch.long)
    )
    torch.testing.assert_close(
        padded["token_type_ids"][:, 5:], torch.zeros(1, 3, dtype=torch.long)
    )
    torch.testing.assert_close(padded["pixel_values"], pixels)


def test_compile_padding_rejects_oversize_instead_of_truncating() -> None:
    with pytest.raises(ValueError, match="exceeds fixed compile prefix length"):
        PadModelInput(max_length=4, pad_token_id=0)(_model_input(5))


def test_compile_padding_keeps_already_static_input_unchanged() -> None:
    model_input = _model_input(5)
    identities = {key: id(value) for key, value in model_input.items()}

    result = PadModelInput(max_length=5, pad_token_id=0)(model_input)

    assert result is model_input
    assert {key: id(value) for key, value in result.items()} == identities
