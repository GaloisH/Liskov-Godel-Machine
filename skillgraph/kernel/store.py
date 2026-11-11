"""Persistence of the library state as a plain directory (readable, diff-able, no extra dependency).

    <root>/
      kernel.json          counters and split parameters
      skills/<Name>_v<version>.yaml
      certs/<Name>_v<version>.json
      traces/<trace id>.json
      tickets.jsonl        append-only: one snapshot per change, the last snapshot of an id wins
      ledger.jsonl         append-only repair ledger
      checks.jsonl         append-only log of the kernel's evidence checks

The statistics layer is not stored: it is a fold over the recorded traces and is rebuilt on load.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from ..model import load_skill
from ..trace import Trace


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=repr)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        for d in ("skills", "certs", "traces"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    @property
    def exists(self) -> bool:
        return (self.root / "kernel.json").exists()

    @staticmethod
    def _file(ref: str) -> str:
        return ref.replace("@", "_v")

    # ------------------------------------------------------------ writes

    def save_state(self, state: dict):
        (self.root / "kernel.json").write_text(_dump(state), encoding="utf-8")

    def save_skill(self, ref: str, skill):
        raw = {**skill.raw, "version": str(skill.version)}
        text = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        (self.root / "skills" / f"{self._file(ref)}.yaml").write_text(text, encoding="utf-8")

    def save_cert(self, ref: str, cert: dict):
        (self.root / "certs" / f"{self._file(ref)}.json").write_text(_dump(cert), encoding="utf-8")

    def save_trace(self, tr: Trace):
        (self.root / "traces" / f"{tr.meta['id']}.json").write_text(tr.to_json(), encoding="utf-8")

    def append(self, name: str, record: dict):
        with (self.root / f"{name}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=repr) + "\n")

    # ------------------------------------------------------------ reads

    def load_state(self) -> dict:
        return json.loads((self.root / "kernel.json").read_text(encoding="utf-8"))

    def load_skills(self) -> list:
        return [load_skill(yaml.safe_load(p.read_text(encoding="utf-8")))
                for p in sorted((self.root / "skills").glob("*.yaml"))]

    def load_certs(self) -> dict:
        certs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.root / "certs").glob("*.json"))]
        return {c["version"]: c for c in certs}

    def load_traces(self) -> list:
        return [Trace.from_dict(json.loads(p.read_text(encoding="utf-8")))
                for p in sorted((self.root / "traces").glob("*.json"))]

    def load_log(self, name: str) -> list:
        p = self.root / f"{name}.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
