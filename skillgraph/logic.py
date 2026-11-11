"""Entailment checking: replaces the SMT queries of the design document.

Method: decidable variables are enumerated over their finite domains; atoms containing predicate calls or opaque variables
(e.g. `relevant(F)`, `len(F) > 0`) are abstracted to free Boolean variables, i.e. a propositional abstraction of uninterpreted functions.
The check is **sound** (anything judged valid is valid) but incomplete with respect to semantic relations between predicates.
"""
from __future__ import annotations

from itertools import product

from .expr import CMP, LOGIC, Bin, Call, ExprError, Lit, Not, SetLit, Var, _walk, apply_op, show


class TooLarge(ExprError):
    pass


def _is_abs(e, types) -> bool:
    return any(isinstance(n, Call) or (isinstance(n, Var) and n.name in types
                                       and not types[n.name].decidable) for n in _walk(e))


def _boolish(e, types) -> bool:
    if isinstance(e, Not) or (isinstance(e, Bin) and e.op in LOGIC | CMP):
        return True
    if isinstance(e, Lit):
        return isinstance(e.value, bool)
    return isinstance(e, Var) and e.name in types and types[e.name].kind == "bool"


def _is_iff(e, types) -> bool:
    return e.op in ("==", "!=") and (_boolish(e.l, types) or _boolish(e.r, types))


def _symbols(e, types, out: set):
    """Collect decidable variable names and atoms (atoms start with "@")."""
    if isinstance(e, Call):
        out.add("@" + show(e))
    elif isinstance(e, Var):
        if e.name in types and types[e.name].decidable:
            out.add(e.name)
    elif isinstance(e, Not):
        _symbols(e.x, types, out)
    elif isinstance(e, SetLit):
        for i in e.items:
            _symbols(i, types, out)
    elif isinstance(e, Bin):
        if e.op in CMP and not _is_iff(e, types) and _is_abs(e, types):
            out.add("@" + show(e))
        else:
            _symbols(e.l, types, out)
            _symbols(e.r, types, out)
    return out


def _abs_eval(e, env, types):
    def ev(x):
        return _abs_eval(x, env, types)

    if isinstance(e, Lit):
        return e.value
    if isinstance(e, Var):
        if e.name in env:
            return env[e.name]
        if e.name in types:
            raise ExprError(f"opaque variable {e.name} cannot take part in the check directly")
        return e.name                       # enum literal
    if isinstance(e, Call):
        return env["@" + show(e)]
    if isinstance(e, SetLit):
        return frozenset(ev(i) for i in e.items)
    if isinstance(e, Not):
        return not ev(e.x)
    if e.op == "&&":
        return ev(e.l) and ev(e.r)
    if e.op == "||":
        return ev(e.l) or ev(e.r)
    if e.op == "->":
        return (not ev(e.l)) or ev(e.r)
    if e.op in CMP and not _is_iff(e, types) and _is_abs(e, types):
        return env["@" + show(e)]
    return apply_op(e.op, ev(e.l), ev(e.r))


def entails(premises, goal, types, limit: int = 1 << 20):
    """Is premises ⊨ goal valid? Returns None if valid, otherwise a counterexample (variable/atom → value)."""
    gsyms = _symbols(goal, types, set())
    info = [(p, _symbols(p, types, set())) for p in premises]
    # Cone of influence: keep only premises that (transitively) share symbols with the goal. Dropping premises only weakens the result, so this is sound.
    rel, used, changed = set(gsyms), [], True
    while changed:
        changed = False
        for item in info:
            if item not in used and (item[1] & rel or not item[1]):
                used.append(item)
                rel |= item[1]
                changed = True
    dvars = sorted(s for s in rel if not s.startswith("@"))
    atoms = sorted(s for s in rel if s.startswith("@"))
    doms = [types[v].domain() for v in dvars] + [[False, True]] * len(atoms)
    size = 1
    for d in doms:
        size *= len(d)
    if size > limit:
        raise TooLarge(f"state space too large ({size}) to enumerate")
    names = dvars + atoms
    for vals in product(*doms):
        env = dict(zip(names, vals))
        if all(_abs_eval(p, env, types) for p, _ in used) and not _abs_eval(goal, env, types):
            return {k.lstrip("@"): v for k, v in env.items()}
    return None


def exactly_one(guards):
    """Build an "exactly one holds" formula (used by W1)."""
    terms = []
    for i, g in enumerate(guards):
        t = g
        for j, h in enumerate(guards):
            if i != j:
                t = Bin("&&", t, Not(h))
        terms.append(t)
    out = terms[0] if terms else Lit(False)
    for t in terms[1:]:
        out = Bin("||", out, t)
    return out


def fmt_cex(cex: dict) -> str:
    def f(v):
        return {True: "true", False: "false"}.get(v, v) if isinstance(v, bool) else v
    return ", ".join(f"{k} = {f(v)}" for k, v in cex.items()) or "(empty state)"
