"""Seed meta skills (Step 2 design doc §3, §9.1): Evolve, Diagnose, Propose, Compare and an ordering-only Audit.

They are ordinary skills: YAML graphs in `seeds/`, admitted through the same gate, run on the same executor.
"""
from __future__ import annotations

from pathlib import Path

from ..model import load_skills
from . import tools as _tools

SEEDS = Path(__file__).resolve().parent / "seeds"


def install(kernel) -> list:
    """Register the seeds' tool functions and predicates, and install the seed skills that are not in the library yet.

    Call it again after loading a kernel from disk: code is not persisted, skills are.
    """
    kernel.register(tools=_tools.tools(kernel), runtime=_tools.RUNTIME)
    missing = [s for s in load_skills(SEEDS) if s.version not in kernel.library.versions(s.name)]
    return kernel.install_all(missing)


__all__ = ["SEEDS", "install"]
