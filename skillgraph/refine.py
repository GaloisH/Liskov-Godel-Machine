"""Interface refinement rules (design doc §8.1): is the new version's interface "no worse" than the old one?"""
from __future__ import annotations

from dataclasses import dataclass, field

from .expr import ExprError, show
from .logic import entails, fmt_cex


@dataclass
class RefineReport:
    old: str
    new: str
    items: list = field(default_factory=list)      # (field, satisfied, explanation)
    budget_changed: bool = False

    @property
    def compatible(self) -> bool:
        return all(ok for _, ok, _ in self.items)

    def __str__(self) -> str:
        lines = [f"{self.old} → {self.new}: {'compatible interface' if self.compatible else 'incompatible interface'}"
                 + (" (budget changed; callers must be re-checked)" if self.budget_changed else "")]
        lines += [f"  {'✓' if ok else '✗'} {name}: {msg}" for name, ok, msg in self.items]
        return "\n".join(lines)


def _implies(premises, goal, types):
    try:
        cex = entails(premises, goal, types)
    except ExprError as ex:
        return False, str(ex)
    return cex is None, "" if cex is None else f"counterexample: {fmt_cex(cex)}"


def check_refinement(old, new) -> RefineReport:
    """old and new are two versions of the same skill."""
    a, b = old.interface, new.interface
    types = {**old.types, **new.types, **a.types, **b.types}
    r = RefineReport(old.key, new.key)

    ok, why = _implies([a.pre], b.pre, types)
    r.items.append(("Pre may only weaken", ok, "old Pre implies new Pre" if ok else f"some input is accepted by the old version but not the new one; {why}"))

    ok, why = _implies([b.post_safe], a.post_safe, types)
    r.items.append(("Post_safe may only strengthen", ok, "new Post implies old Post" if ok else
                    f"the new version no longer guarantees {show(a.post_safe)}; {why}"))

    lost = [show(i) for i in a.inv if not _implies(b.inv, i, types)[0]]
    r.items.append(("Inv may only strengthen", not lost, "all old invariants kept" if not lost else f"invariants lost: {lost}"))

    bad = [k for k, t in a.inputs.items() if b.inputs.get(k) != t]
    bad += [k for k in b.inputs if k not in a.inputs and k not in b.defaults]
    r.items.append(("X_in may not add required inputs", not bad, "inputs unchanged (or only inputs with defaults added)" if not bad
                    else f"inputs {bad} were removed, retyped, or added as required"))

    bad = [k for k, t in a.outputs.items() if b.outputs.get(k) != t]
    r.items.append(("X_out may only grow", not bad, "all old outputs kept" if not bad else f"outputs {bad} were removed or retyped"))

    r.budget_changed = a.budget != b.budget
    return r
