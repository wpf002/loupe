"""Float16 vector file on disk. Row numbers are assigned in the `embeddings` table."""

import os
from pathlib import Path

import numpy as np

VECTOR_DTYPE = np.float16
MIN_CAPACITY = 1024


class VectorStoreError(Exception):
    """Raised when the vector file does not match the model it is opened for."""


class VectorStore:
    """A growable (capacity, dim) float16 .npy memmap. Unused rows are zeros."""

    def __init__(self, path: Path, dim: int) -> None:
        self.path = path
        self.dim = dim
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self._array = np.lib.format.open_memmap(path, mode="r+")
            if self._array.dtype != VECTOR_DTYPE or self._array.shape[1] != dim:
                raise VectorStoreError(
                    f"{path.name} holds {self._array.shape[1]}-d {self._array.dtype} vectors, "
                    f"expected {dim}-d {np.dtype(VECTOR_DTYPE)}."
                )
        else:
            self._array = self._create(path, MIN_CAPACITY)

    def _create(self, path: Path, capacity: int) -> np.memmap:
        return np.lib.format.open_memmap(
            path, mode="w+", dtype=VECTOR_DTYPE, shape=(capacity, self.dim)
        )

    @property
    def capacity(self) -> int:
        return self._array.shape[0]

    def ensure_capacity(self, rows: int) -> None:
        if rows <= self.capacity:
            return
        new_capacity = max(rows, self.capacity * 2)
        staging = self.path.with_suffix(".grow.npy")
        grown = self._create(staging, new_capacity)
        grown[: self.capacity] = self._array
        grown.flush()
        del self._array
        os.replace(staging, self.path)
        self._array = grown

    def write(self, first_row: int, vectors: np.ndarray) -> None:
        self.ensure_capacity(first_row + len(vectors))
        self._array[first_row : first_row + len(vectors)] = vectors.astype(VECTOR_DTYPE)
        self._array.flush()

    def clear(self, rows: list[int]) -> None:
        self._array[rows] = 0
        self._array.flush()

    def read(self, rows: list[int]) -> np.ndarray:
        return np.asarray(self._array[rows])
