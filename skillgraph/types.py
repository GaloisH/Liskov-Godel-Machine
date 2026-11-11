"""黑板变量的类型（设计文档 §2）。

可判定类型（bool / 枚举 / 有界整数 / 有限集合）可以出现在边条件里，并且定义域可以穷举；
不透明类型（文本、补丁、文件……）不能出现在边条件里。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations


@dataclass(frozen=True)
class Type:
    kind: str                 # bool | enum | int | set | opaque
    values: tuple = ()        # enum 的取值 / set 的全集
    lo: int = 0
    hi: int = 0
    name: str = ""            # 不透明类型的名字

    @property
    def decidable(self) -> bool:
        return self.kind != "opaque"

    def domain(self) -> list:
        """可判定类型的全部取值。"""
        if self.kind == "bool":
            return [False, True]
        if self.kind == "enum":
            return list(self.values)
        if self.kind == "int":
            return list(range(self.lo, self.hi + 1))
        if self.kind == "set":
            return [frozenset(c) for n in range(len(self.values) + 1)
                    for c in combinations(self.values, n)]
        raise TypeError(f"不透明类型 {self.name} 没有可穷举的定义域")

    def contains(self, v) -> bool:
        if self.kind == "bool":
            return isinstance(v, bool)
        if self.kind == "enum":
            return v in self.values
        if self.kind == "int":
            return isinstance(v, int) and not isinstance(v, bool) and self.lo <= v <= self.hi
        if self.kind == "set":
            return isinstance(v, (set, frozenset, list, tuple)) and set(v) <= set(self.values)
        return True

    def __str__(self) -> str:
        if self.kind == "bool":
            return "bool"
        if self.kind == "enum":
            return "{" + ", ".join(self.values) + "}"
        if self.kind == "int":
            return f"int[{self.lo}..{self.hi}]"
        if self.kind == "set":
            return "set{" + ", ".join(self.values) + "}"
        return self.name


BOOL = Type("bool")
OUTCOMES = ("done", "give_up", "reject")
OUTCOME_T = Type("enum", values=OUTCOMES)

_INT = re.compile(r"^int\[\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*\]$")
_ENUM = re.compile(r"^(enum|set)?\s*\{(.*)\}$")


def parse_type(spec) -> Type:
    """"bool" / "int[0..3]" / "{low, mid, high}" / "set{a, b}" / 其他一律视为不透明类型。"""
    if isinstance(spec, Type):
        return spec
    # YAML 里没加引号的 {a, b} 会被解析成 dict，[a, b] 会被解析成 list，这里一并接受
    if isinstance(spec, dict) and "type" not in spec:
        return Type("enum", values=tuple(str(k) for k in spec))
    if isinstance(spec, (list, tuple)):
        return Type("enum", values=tuple(str(k) for k in spec))
    s = str(spec).strip()
    if s == "bool":
        return BOOL
    if m := _INT.match(s):
        lo, hi = int(m[1]), int(m[2])
        if lo > hi:
            raise ValueError(f"非法整数区间: {s}")
        return Type("int", lo=lo, hi=hi)
    if m := _ENUM.match(s):
        vals = tuple(v.strip() for v in m[2].split(",") if v.strip())
        if not vals:
            raise ValueError(f"空枚举: {s}")
        return Type("set" if m[1] == "set" else "enum", values=vals)
    return Type("opaque", name=s)
