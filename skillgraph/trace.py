"""Run traces (design doc §7.2): the only source of evidence for evolution."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from .expr import UNKNOWN


class TV(str, Enum):
    """Three-valued result of a contract check."""
    T = "⊤"
    F = "⊥"
    U = "?"

    @staticmethod
    def of(v) -> "TV":
        return TV.U if v is UNKNOWN or not isinstance(v, bool) else (TV.T if v else TV.F)

    def __str__(self):
        return self.value


def digest(obj) -> str:
    """Content hash: the key for cached replay."""
    s = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=repr)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


@dataclass
class Step:
    t: int
    node: str
    kind: str
    input_view: dict = field(default_factory=dict)   # blackboard contents of the read set
    input_hash: str = ""
    prompt_hash: str = ""                            # agent only
    output: dict = field(default_factory=dict)
    contract_pre: TV | None = None
    contract_post: TV | None = None                  # for blame assignment
    inv: TV | None = None
    cost: dict = field(default_factory=dict)         # llm_calls / seconds
    next: str | None = None                          # which outgoing edge was taken
    sub_trace: "Trace | None" = None                 # sub-trace of a skill call node

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("t", "node", "kind", "input_view", "input_hash", "prompt_hash",
                                           "output", "cost", "next")}
        d.update(contract_pre=_s(self.contract_pre), contract_post=_s(self.contract_post), inv=_s(self.inv),
                 sub_trace=self.sub_trace.to_dict() if self.sub_trace else None)
        return d

    @staticmethod
    def from_dict(d: dict) -> "Step":
        return Step(t=d["t"], node=d["node"], kind=d["kind"], input_view=d.get("input_view") or {},
                    input_hash=d.get("input_hash", ""), prompt_hash=d.get("prompt_hash", ""),
                    output=d.get("output") or {}, contract_pre=_tv(d.get("contract_pre")),
                    contract_post=_tv(d.get("contract_post")), inv=_tv(d.get("inv")), cost=d.get("cost") or {},
                    next=d.get("next"), sub_trace=Trace.from_dict(d["sub_trace"]) if d.get("sub_trace") else None)


def _s(tv):
    return None if tv is None else str(tv)


def _tv(s):
    return None if s is None else TV(s)


@dataclass
class Trace:
    skill: str
    version: str
    inputs: dict
    pre: TV | None = None
    steps: list = field(default_factory=list)
    outcome: str | None = None        # done / give_up / reject; None when a replay stops at the frontier
    outputs: dict = field(default_factory=dict)
    status: str = "ok"                # ok / pre_violated / inv_violation / budget_exceeded / stuck
                                      # / type_error / tool_error / frontier
    detail: str = ""
    frontier: dict | None = None      # divergence frontier of a cached replay
    post_eval: dict | None = None     # post-hoc evaluation: post_safe / post_live / inv
    meta: dict = field(default_factory=dict)    # kernel bookkeeping: id / task / split / seq / virtual

    @property
    def llm_calls(self) -> int:
        return sum(s.cost.get("llm_calls", 0) for s in self.steps)

    @property
    def exact(self) -> bool:
        """Did the replay reach a terminal (without needing new agent input)?"""
        return self.status != "frontier"

    def to_dict(self) -> dict:
        return {"skill": self.skill, "version": self.version, "inputs": self.inputs, "pre": _s(self.pre),
                "steps": [s.to_dict() for s in self.steps], "outcome": self.outcome, "outputs": self.outputs,
                "status": self.status, "detail": self.detail, "frontier": self.frontier,
                "post_eval": {k: _s(v) for k, v in self.post_eval.items()} if self.post_eval else None,
                "meta": self.meta}

    @staticmethod
    def from_dict(d: dict) -> "Trace":
        """Inverse of to_dict. Blackboard values must be JSON-serialisable to survive the round trip."""
        return Trace(skill=d["skill"], version=d["version"], inputs=d.get("inputs") or {}, pre=_tv(d.get("pre")),
                     steps=[Step.from_dict(x) for x in d.get("steps") or []], outcome=d.get("outcome"),
                     outputs=d.get("outputs") or {}, status=d.get("status", "ok"), detail=d.get("detail", ""),
                     frontier=d.get("frontier"),
                     post_eval={k: _tv(v) for k, v in d["post_eval"].items()} if d.get("post_eval") else None,
                     meta=d.get("meta") or {})

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=repr)

    def pretty(self, indent: int = 0) -> str:
        pad = " " * indent
        lines = [f"{pad}[{self.skill} v{self.version}] Pre = {_s(self.pre)}"]
        for s in self.steps:
            if s.kind == "terminal":
                lines.append(f"{pad}  t{s.t} {s.node}")
                continue
            dec = {k: v for k, v in s.output.items() if isinstance(v, (bool, int, str)) and 0 < len(str(v)) < 24}
            bits = [f"t{s.t} {s.node} ({s.kind})"]
            if s.contract_post is not None:
                bits.append(f"contract {s.contract_post}")
            if s.inv is TV.F:
                bits.append("Inv ⊥")
            if dec:
                bits.append(" ".join(f"{k}={v}" for k, v in dec.items()))
            if s.next:
                bits.append(f"→ {s.next}")
            lines.append(f"{pad}  " + "  ".join(bits))
            if s.sub_trace:
                lines.append(s.sub_trace.pretty(indent + 6))
        if self.status == "frontier":
            f = self.frontier
            lines.append(f"{pad}  ── divergence frontier: {f['skill']}.{f['node']} needs new agent output; everything before it is an exact counterfactual")
        else:
            tail = f"{pad}  outcome: {self.outcome}"
            if self.status != "ok":
                tail += f" ({self.status}: {self.detail})"
            if self.post_eval:
                tail += "; post-hoc evaluation " + ", ".join(f"{k} = {v}" for k, v in self.post_eval.items())
            lines.append(tail + f"; model calls: {self.llm_calls}")
        return "\n".join(lines)
