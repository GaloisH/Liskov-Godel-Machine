"""Types of blackboard variables (design doc §2).

Decidable types (bool / enum / bounded int / finite set) may appear in edge conditions and have enumerable domains;
opaque types (text, patches, files, ...) may not appear in edge conditions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations


@dataclass(frozen=True)
class Type:
    kind: str                 # bool | enum | int | set | opaque
    values: tuple = ()        # values of an enum / universe of a set
    lo: int = 0
    hi: int = 0
    name: str = ""            # name of an opaque type

    @property
    def decidable(self) -> bool:
        return self.kind != "opaque"

    def domain(self) -> list:
        """All values of a decidable type."""
        if self.kind == "bool":
            return [False, True]
        if self.kind == "enum":
            return list(self.values)
        if self.kind == "int":
            return list(range(self.lo, self.hi + 1))
        if self.kind == "set":
            return [frozenset(c) for n in range(len(self.values) + 1)
                    for c in combinations(self.values, n)]
        raise TypeError(f"opaque type {self.name} has no enumerable domain")

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
    """"bool" / "int[0..3]" / "{low, mid, high}" / "set{a, b}" / anything else is an opaque type."""
    if isinstance(spec, Type):
        return spec
    # unquoted {a, b} in YAML is parsed as a dict and [a, b] as a list; accept both here
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
            raise ValueError(f"invalid integer range: {s}")
        return Type("int", lo=lo, hi=hi)
    if m := _ENUM.match(s):
        vals = tuple(v.strip() for v in m[2].split(",") if v.strip())
        if not vals:
            raise ValueError(f"empty enum: {s}")
        return Type("set" if m[1] == "set" else "enum", values=vals)
    return Type("opaque", name=s)
