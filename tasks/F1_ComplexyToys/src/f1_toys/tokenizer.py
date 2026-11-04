"""Offline Pi0.5 prompt/state encoding, checked against OpenPI's tokenizer.

Protocol reference: Physical-Intelligence/openpi src/openpi/models/tokenizer.py
(Apache-2.0). This small adapter needs only the official SentencePiece asset;
inference does not import JAX, Orbax or download weights on startup.
"""
import os
from pathlib import Path

import numpy as np
import sentencepiece


class Tokenizer:
    def __init__(self, max_len=200, path=None):
        path = path or os.environ.get("F1_TOYS_TOKENIZER")
        if not path:
            raise ValueError("Set F1_TOYS_TOKENIZER to the verified PaliGemma asset")
        self.max_len = max_len
        self.model = sentencepiece.SentencePieceProcessor(model_proto=Path(path).read_bytes())

    def tokenize(self, prompt, state):
        text = prompt.strip().replace("_", " ").replace("\n", " ")
        discrete = np.digitize(state, bins=np.linspace(-1, 1, 257)[:-1]) - 1
        state_text = " ".join(map(str, discrete))
        tokens = self.model.encode(f"Task: {text}, State: {state_text};\nAction: ", add_bos=True)
        if len(tokens) > self.max_len:
            raise ValueError("Task/state prompt exceeds model token budget; refusing silent truncation")
        mask = [True] * len(tokens) + [False] * (self.max_len - len(tokens))
        return np.asarray(tokens + [0] * (self.max_len - len(tokens))), np.asarray(mask)
