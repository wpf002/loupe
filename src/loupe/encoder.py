"""SigLIP image encoder through open_clip, on the MPS device."""

from collections.abc import Sequence
from typing import Any

import numpy as np
from PIL import Image

MODEL_NAME = "ViT-SO400M-16-SigLIP2-384"
PRETRAINED = "webli"
DEVICE = "mps"
PRECISION = "fp16"
MODEL_ID = f"{MODEL_NAME}/{PRETRAINED}"


class SiglipEncoder:
    def __init__(self, device: str = DEVICE) -> None:
        import open_clip
        import torch

        self._torch = torch
        self.device = device
        self.model_id = MODEL_ID
        self.model, _, self._preprocess = open_clip.create_model_and_transforms(
            MODEL_NAME, pretrained=PRETRAINED, device=device, precision=PRECISION
        )
        self.model.eval()
        self.dtype = next(self.model.parameters()).dtype
        self.tokenizer = open_clip.get_tokenizer(MODEL_NAME)
        self.dim = self._probe_dim()

    def _probe_dim(self) -> int:
        blank = Image.new("RGB", (32, 32))
        return int(self.encode([self.preprocess(blank)]).shape[1])

    def preprocess(self, image: Image.Image) -> Any:
        return self._preprocess(image)

    def encode(self, batch: Sequence[Any]) -> np.ndarray:
        pixels = self._torch.stack(list(batch)).to(self.device, dtype=self.dtype)
        with self._torch.inference_mode():
            features = self.model.encode_image(pixels, normalize=True)
        return features.float().cpu().numpy()

    def encode_texts(self, texts: Sequence[str]) -> np.ndarray:
        tokens = self.tokenizer(list(texts)).to(self.device)
        with self._torch.inference_mode():
            features = self.model.encode_text(tokens, normalize=True)
        return features.float().cpu().numpy()
