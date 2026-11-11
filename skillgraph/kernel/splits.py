"""Data split held by the kernel (Step 2 design doc §2, §9.2).

A task id is mapped to train / heldout / test by a seeded hash, so the assignment is stable across runs and cannot be
influenced by a meta skill. Only train runs open tickets and count as evidence; heldout is for kernel scoring,
test is for the final report.
"""
from __future__ import annotations

import hashlib

SPLITS = ("train", "heldout", "test")


class Splits:
    def __init__(self, seed: int = 0, ratios=(0.6, 0.2, 0.2)):
        if len(ratios) != 3 or abs(sum(ratios) - 1) > 1e-9 or min(ratios) < 0:
            raise ValueError(f"ratios must be three non-negative numbers summing to 1: {ratios}")
        self.seed, self.ratios = seed, tuple(ratios)

    def of(self, task) -> str:
        """Split of a task id. A run without a task id is an ad-hoc run and counts as train."""
        if task is None:
            return "train"
        h = hashlib.sha256(f"{self.seed}:{task}".encode("utf-8")).digest()
        u = int.from_bytes(h[:8], "big") / 2 ** 64
        return "train" if u < self.ratios[0] else "heldout" if u < self.ratios[0] + self.ratios[1] else "test"
