"""运行轨迹（设计文档 §7.2）：进化的唯一证据来源。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from .expr import UNKNOWN


class TV(str, Enum):
    """契约检查的三值结果。"""
    T = "⊤"
    F = "⊥"
    U = "?"

    @staticmethod
    def of(v) -> "TV":
        return TV.U if v is UNKNOWN or not isinstance(v, bool) else (TV.T if v else TV.F)

    def __str__(self):
        return self.value


def digest(obj) -> str:
    """内容哈希：缓存重放的键。"""
    s = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=repr)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


@dataclass
class Step:
    t: int
    node: str
    kind: str
    input_view: dict = field(default_factory=dict)   # 读集对应的黑板内容
    input_hash: str = ""
    prompt_hash: str = ""                            # 仅 agent
    output: dict = field(default_factory=dict)
    contract_pre: TV | None = None
    contract_post: TV | None = None                  # 归责用
    inv: TV | None = None
    cost: dict = field(default_factory=dict)         # llm_calls / seconds
    next: str | None = None                          # 走了哪条出边
    sub_trace: "Trace | None" = None                 # Skill 调用节点的子轨迹

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("t", "node", "kind", "input_view", "input_hash", "prompt_hash",
                                           "output", "cost", "next")}
        d.update(contract_pre=_s(self.contract_pre), contract_post=_s(self.contract_post), inv=_s(self.inv),
                 sub_trace=self.sub_trace.to_dict() if self.sub_trace else None)
        return d


def _s(tv):
    return None if tv is None else str(tv)


@dataclass
class Trace:
    skill: str
    version: str
    inputs: dict
    pre: TV | None = None
    steps: list = field(default_factory=list)
    outcome: str | None = None        # done / give_up / reject；重放停在前沿时为 None
    outputs: dict = field(default_factory=dict)
    status: str = "ok"                # ok / pre_violated / inv_violation / budget_exceeded / stuck
                                      # / type_error / tool_error / frontier
    detail: str = ""
    frontier: dict | None = None      # 缓存重放的分歧前沿
    post_eval: dict | None = None     # 事后评估：post_safe / post_live / inv

    @property
    def llm_calls(self) -> int:
        return sum(s.cost.get("llm_calls", 0) for s in self.steps)

    @property
    def exact(self) -> bool:
        """重放是否一路走到终点（没有碰到新的 Agent 输入）。"""
        return self.status != "frontier"

    def to_dict(self) -> dict:
        return {"skill": self.skill, "version": self.version, "inputs": self.inputs, "pre": _s(self.pre),
                "steps": [s.to_dict() for s in self.steps], "outcome": self.outcome, "outputs": self.outputs,
                "status": self.status, "detail": self.detail, "frontier": self.frontier,
                "post_eval": {k: _s(v) for k, v in self.post_eval.items()} if self.post_eval else None}

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
            bits = [f"t{s.t} {s.node}（{s.kind}）"]
            if s.contract_post is not None:
                bits.append(f"契约 {s.contract_post}")
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
            lines.append(f"{pad}  ── 分歧前沿: {f['skill']}.{f['node']} 需要新的 Agent 输出，之前的结果是精确的反事实")
        else:
            tail = f"{pad}  结果: {self.outcome}"
            if self.status != "ok":
                tail += f"（{self.status}: {self.detail}）"
            if self.post_eval:
                tail += "；事后评估 " + ", ".join(f"{k} = {v}" for k, v in self.post_eval.items())
            lines.append(tail + f"；模型调用 {self.llm_calls} 次")
        return "\n".join(lines)
