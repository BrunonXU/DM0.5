"""CPU tensor boundary check using unchanged shared class definitions.

The audit host lacks optional megfile/albumentations. Extract only the exact
shared tokenizer/collator AST classes, so unrelated imports are unnecessary.
The processor is a small test double; this is not model inference validation.
"""
import ast
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")
from test_reader import make_episode
from f1_data.reader import F1Dataset, Normalizer, fit_statistics


def shared_class(relative, name):
    root = next(parent for parent in Path(__file__).resolve().parents if (parent / "DM0.5/opendm/opendm").exists())
    path = root / "DM0.5/opendm/opendm/data" / relative
    tree = ast.parse(path.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    namespace = {"np": np, "torch": torch, "Sequence": Sequence}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class Processor:
    def apply_chat_template(self, messages, **kwargs):
        content = messages[0]["content"]
        assert sum(item["type"] == "image" for item in content) == 3
        state = content[-1]["text"].removeprefix("States: ").split()
        assert len(state) == 19 and all(0 <= int(value) <= 255 for value in state)
        return {"input_ids": torch.ones((1, 100), dtype=torch.long),
                "attention_mask": torch.ones((1, 100), dtype=torch.long),
                "token_type_ids": torch.zeros((1, 100), dtype=torch.long),
                "pixel_values": torch.zeros((3, 3, 8, 8))}


def test_exact_shared_tokenizer_collator_shapes(tmp_path):
    ep = make_episode(tmp_path)
    tokenizer = shared_class("transforms.py", "ChatTokenization")(Processor(), image_keys=["images_1", "images_2", "images_3"])
    dataset = F1Dataset([ep], Normalizer(fit_statistics([ep])),
                        lambda ep, indices: {f"images_{i+1}": object() for i in range(3)}, tokenizer)
    collator = shared_class("collator.py", "TrainingCollator")(pad_token_id=0)
    batch = collator([dataset[0], dataset[1]])
    assert tuple(batch["action"].shape) == (2, 50, 32)
    assert tuple(batch["action_mask"].shape) == (2, 50, 32)
    assert batch["action_mask"].dtype == torch.bool
    assert tuple(batch["input_ids"].shape) == (2, 1024)
    assert not batch["action_mask"][..., 23:].any()
    assert torch.isfinite(batch["action"]).all()


def test_partial_base_unknown_tokens_and_native_collator_mask(tmp_path):
    from test_partial_base_reader import promote
    from f1_data.state_tokens import make_tokenizer
    ep = promote(make_episode(tmp_path, gap=55))

    class MissingProcessor:
        def apply_chat_template(self, messages, **kwargs):
            state = messages[0]['content'][-1]['text'].removeprefix('States: ').split()
            assert len(state) == 19
            assert all(0 <= int(value) <= 255 for value in state[:16])
            assert state[-3:] == ['unknown'] * 3
            return {'input_ids': torch.ones((1, 100), dtype=torch.long),
                    'attention_mask': torch.ones((1, 100), dtype=torch.long),
                    'token_type_ids': torch.zeros((1, 100), dtype=torch.long),
                    'pixel_values': torch.zeros((3, 3, 8, 8))}

    tokenizer = make_tokenizer(MissingProcessor(), base_class=shared_class('transforms.py', 'ChatTokenization'),
                               image_keys=['images_1', 'images_2', 'images_3'])
    dataset = F1Dataset([ep], Normalizer(fit_statistics([ep])),
                        lambda ep, indices: {f'images_{i+1}': object() for i in range(3)}, tokenizer)
    collator = shared_class('collator.py', 'TrainingCollator')(pad_token_id=0)
    batch = collator([dataset[55], dataset[55]])
    assert batch['action_mask'][:, :, :20].all()
    assert not batch['action_mask'][:, 0, 20:23].any()
    assert batch['action_mask'][:, 1:, 20:23].all()
    assert not batch['action_mask'][:, :, 23:].any()
