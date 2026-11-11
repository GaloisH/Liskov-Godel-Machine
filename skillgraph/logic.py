"""蕴含判定：代替设计文档里的 SMT 查询。

做法：可判定变量在有限定义域上穷举；含谓词调用或不透明变量的原子公式
（如 `relevant(F)`、`len(F) > 0`）抽象成自由布尔变量，相当于未解释函数的命题抽象。
这个判定是**可靠**的（判为有效就一定有效），但对谓词之间的语义关系不完备。
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
    """收集可判定变量名和原子公式（原子以 "@" 开头）。"""
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
            raise ExprError(f"不透明变量 {e.name} 不能直接参与判定")
        return e.name                       # 枚举字面量
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
    """premises ⊨ goal 是否有效。有效返回 None，否则返回一个反例（变量/原子 → 取值）。"""
    gsyms = _symbols(goal, types, set())
    info = [(p, _symbols(p, types, set())) for p in premises]
    # 影响锥：只保留和目标（传递地）共享符号的前提。丢掉前提只会让结论更弱，所以是可靠的。
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
        raise TooLarge(f"状态空间过大（{size}），无法穷举")
    names = dvars + atoms
    for vals in product(*doms):
        env = dict(zip(names, vals))
        if all(_abs_eval(p, env, types) for p, _ in used) and not _abs_eval(goal, env, types):
            return {k.lstrip("@"): v for k, v in env.items()}
    return None


def exactly_one(guards):
    """构造"恰好一个成立"的公式（W1 用）。"""
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
    return ", ".join(f"{k} = {f(v)}" for k, v in cex.items()) or "（空状态）"
