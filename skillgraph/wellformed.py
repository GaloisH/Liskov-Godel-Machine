"""良构性检查 W0–W8 与静态预算检查（设计文档 §6）。全部自动、不依赖 Agent。

W0  结构与类型：引用都有声明、边条件只读可判定变量、契约只引用节点的读写集……
W1  确定且完全   W2  可达   W3  终止   W4  先写后读
W5  不变量保持   W6  调用接口匹配   W7  调用图无环
B   预算：最坏情况下的模型调用次数不超过声明的预算
W8  规约一致（可选）：在所有节点守约的前提下，Pre 出发一定满足 Post_safe

W5 / W6 / W8 共用一个"事实传播"的前向数据流分析：每个节点守约后留下它的后置条件，
每条边留下它的守卫；变量一旦被改写，提到它的事实就失效；汇合处取交集。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .expr import Bin, ExprError, Lit, Var, calls_of, conjuncts, show, subst, vars_of
from .logic import _abs_eval, entails, exactly_one, fmt_cex
from .types import OUTCOMES

HARD = ("W0", "W1", "W2", "W3", "W4", "W5", "W6", "W7", "B")
NAMES = {"W0": "结构与类型", "W1": "确定且完全", "W2": "可达", "W3": "终止", "W4": "先写后读",
         "W5": "不变量保持", "W6": "调用接口匹配", "W7": "调用图无环", "B": "预算", "W8": "规约一致"}


@dataclass
class Check:
    code: str
    ok: bool
    messages: list = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.code == "W8":
            return "已证明" if self.ok else "未证明"
        return "通过" if self.ok else "不通过"


@dataclass
class Report:
    skill: str
    checks: list

    @property
    def ok(self) -> bool:
        """硬性要求（W0–W7 与预算）是否全部通过。W8 不影响。"""
        return all(c.ok for c in self.checks if c.code in HARD)

    @property
    def w8(self) -> bool:
        return any(c.code == "W8" and c.ok for c in self.checks)

    def __getitem__(self, code: str) -> Check:
        return next(c for c in self.checks if c.code == code)

    def failed(self) -> list:
        return [c.code for c in self.checks if not c.ok and c.code in HARD]

    def __str__(self) -> str:
        lines = [f"{self.skill}: {'合法' if self.ok else '不合法'}"
                 f"（规约一致性{'已证明' if self.w8 else '未证明'}）"]
        for c in self.checks:
            lines.append(f"  {c.code:<2} {NAMES[c.code]:　<6} {c.status}")
            lines += [f"       - {m}" for m in c.messages]
        return "\n".join(lines)


# ---------------------------------------------------------------- 通用工具

def _reach(skill, start: str, forward: bool = True) -> set:
    """从 start 出发至少走一步能到的节点。"""
    seen, todo = set(), [start]
    while todo:
        v = todo.pop()
        for e in (skill.out_edges(v) if forward else skill.in_edges(v)):
            w = e.dst if forward else e.src
            if w not in seen:
                seen.add(w)
                todo.append(w)
    return seen


def _forward(skill, init, node_tf, edge_tf) -> dict:
    """前向 must 分析：格是"集合 + 交集"，未到达的节点为 None。"""
    IN = {v: None for v in skill.nodes}
    IN[skill.entry] = frozenset(init)
    changed = True
    while changed:
        changed = False
        for v in skill.nodes:
            if IN[v] is None:
                continue
            out = node_tf(v, IN[v])
            for e in skill.out_edges(v):
                f = edge_tf(e, out)
                new = f if IN[e.dst] is None else IN[e.dst] & f
                if new != IN[e.dst]:
                    IN[e.dst], changed = new, True
    return IN


def _callee_facts(node, callee) -> list:
    """把被调 skill 的 Post_safe 和 Inv 翻译到调用方的变量上。"""
    m = {k: Var(v) for k, v in {**node.bind_in, **node.bind_out}.items()}
    m["outcome"] = Var(node.outcome_var)
    known = set(callee.interface.types)
    out = []
    for c in conjuncts(callee.interface.post_safe) + [x for i in callee.interface.inv for x in conjuncts(i)]:
        if vars_of(c) & known <= set(m):          # 没绑定的被调变量，调用方看不到，丢弃
            out.append(subst(c, m))
    return out


def _post_facts(skill, node, resolver) -> list:
    if node.kind in ("agent", "tool"):
        return conjuncts(node.contract.post)
    if node.kind == "call":
        callee = resolver(node.skill, node.min_version) if resolver else None
        return _callee_facts(node, callee) if callee else []
    return []


class _Facts:
    """事实传播的结果：每条边上、每个节点入口处成立的事实集合。"""

    def __init__(self, skill, resolver):
        self.skill = skill
        self.init = frozenset(conjuncts(skill.interface.pre))
        self.post = {v: _post_facts(skill, n, resolver) for v, n in skill.nodes.items()}
        self.IN = _forward(skill, self.init, self._node, self._edge)

    def _node(self, v, facts):
        w = set(self.skill.nodes[v].writes)
        return frozenset(f for f in facts if not vars_of(f) & w) | frozenset(self.post[v])

    def _edge(self, e, out):
        f = out | frozenset(conjuncts(e.guard))
        return frozenset(x for x in f if e.counter not in vars_of(x)) if e.counter else f

    def arriving(self, v) -> list:
        """到达节点 v 的每条路径（按入边区分）上成立的事实。比入口处的交集更精确。"""
        out = [("入口", self.init)] if v == self.skill.entry else []
        for e in self.skill.in_edges(v):
            if self.IN[e.src] is not None:
                out.append((f"{e.src} → {v}", self._edge(e, self._node(e.src, self.IN[e.src]))))
        return out

    def prove(self, v, goal, types, shown=None) -> list:
        """在到达 v 的每条路径上证明 goal。返回证明不了的路径及反例。"""
        fails, shown = [], shown or show(goal)
        for label, facts in self.arriving(v):
            try:
                cex = entails(list(facts), goal, types)
            except ExprError as ex:
                fails.append(f"经 {label}: {ex}")
                continue
            if cex is not None:
                fails.append(f"经 {label}: 证明不了 {shown}；反例: {fmt_cex(cex)}")
        return fails


# ---------------------------------------------------------------- 各项检查

def _w0(skill) -> Check:
    msgs, T, I = [], skill.types, skill.interface
    syms = {v for t in T.values() for v in t.values} | set(OUTCOMES)

    def chk(e, allowed, where, guard=False, universe=T):
        for n in sorted(vars_of(e)):
            if n in universe:
                if n not in allowed:
                    msgs.append(f"{where}: 不能引用变量 {n}")
                elif guard and not universe[n].decidable:
                    msgs.append(f"{where}: 边条件只能读可判定类型，{n} 是 {universe[n]}")
            elif n not in syms:
                msgs.append(f"{where}: 未声明的标识符 {n}")
        if guard and calls_of(e):
            msgs.append(f"{where}: 边条件不能调用谓词 {sorted(calls_of(e))}，请改成 Agent 节点输出枚举值")

    if skill.entry not in skill.nodes:
        msgs.append(f"入口节点 {skill.entry} 不存在")
    for v, n in skill.nodes.items():
        for x in (*n.reads, *n.writes):
            if x not in T:
                msgs.append(f"节点 {v}: 读写集里的变量 {x} 未声明")
        for x in n.writes:
            if x in I.inputs:
                msgs.append(f"节点 {v}: 不能改写输入变量 {x}")
            if x in skill.counters:
                msgs.append(f"节点 {v}: 不能改写循环计数器 {x}（计数器只由执行器在回边上加一）")
        outs = skill.out_edges(v)
        if n.kind == "terminal" and outs:
            msgs.append(f"终点 {v} 不能有出边")
        if n.kind != "terminal" and not outs:
            msgs.append(f"节点 {v} 没有出边")
        if n.kind in ("agent", "tool"):
            chk(n.contract.pre, set(n.reads), f"节点 {v} 的前置条件")
            chk(n.contract.post, set(n.reads) | set(n.writes), f"节点 {v} 的后置条件")
    for e in skill.edges:
        for x in (e.src, e.dst):
            if x not in skill.nodes:
                msgs.append(f"边 {e.src} → {e.dst}: 节点 {x} 不存在")
        chk(e.guard, set(T), f"边 {e.src} → {e.dst}", guard=True)
        if e.counter and (e.counter not in T or T[e.counter].kind != "int" or e.counter in I.inputs):
            msgs.append(f"边 {e.src} → {e.dst}: 计数器 {e.counter} 必须是非输入的有界整数")
    for x in I.outputs:
        if x in I.inputs:
            msgs.append(f"变量 {x} 不能同时是输入和输出")
    it = I.types
    chk(I.pre, set(I.inputs), "Pre", universe=it)
    chk(I.post_safe, set(it), "Post_safe", universe=it)
    chk(I.post_live, set(it), "Post_live", universe=it)
    for inv in I.inv:
        chk(inv, set(T), f"Inv {show(inv)}")
    return Check("W0", not msgs, msgs)


def _w1(skill) -> Check:
    msgs, T = [], skill.types
    for v, n in skill.nodes.items():
        guards = [e.guard for e in skill.out_edges(v)]
        if n.kind == "terminal" or not guards:
            continue
        try:
            cex = entails(conjuncts(n.contract.post), exactly_one(guards), T)
        except ExprError as ex:
            msgs.append(f"节点 {v}: {ex}")
            continue
        if cex is not None:
            env = {k: val for k, val in cex.items() if k in T}
            hit = [show(g) for g in guards if _abs_eval(g, env, T)]
            what = "没有出边成立（会卡住）" if not hit else f"同时有 {len(hit)} 条出边成立: {hit}"
            msgs.append(f"节点 {v}: 状态 {fmt_cex(env)} 下{what}")
    return Check("W1", not msgs, msgs)


def _w2(skill) -> Check:
    msgs = []
    fwd = _reach(skill, skill.entry) | {skill.entry}
    terms = [v for v, n in skill.nodes.items() if n.kind == "terminal"]
    if not terms:
        msgs.append("没有终点")
    back = set(terms)
    for t in terms:
        back |= _reach(skill, t, forward=False)
    for v in skill.nodes:
        if v not in fwd:
            msgs.append(f"节点 {v} 从入口不可达")
        elif v not in back:
            msgs.append(f"节点 {v} 走不到任何终点")
    return Check("W2", not msgs, msgs)


def _w3(skill) -> Check:
    msgs, T = [], skill.types
    # 去掉带计数器的边之后，图必须无环
    plain = {v: [e.dst for e in skill.out_edges(v) if not e.counter] for v in skill.nodes}
    state = {}

    def dfs(v, path):
        state[v] = 1
        for w in plain.get(v, ()):
            if state.get(w) == 1:
                msgs.append(f"循环 {' → '.join(path[path.index(w):] + [w])} 上没有带计数器的边")
            elif w not in state:
                dfs(w, path + [w])
        state[v] = 2

    for v in skill.nodes:
        if v not in state:
            dfs(v, [v])
    for e in skill.edges:
        if e.counter and e.counter in T and T[e.counter].kind == "int":
            hi = T[e.counter].hi
            try:
                if entails([e.guard], Bin("<", Var(e.counter), Lit(hi)), T) is not None:
                    msgs.append(f"边 {e.src} → {e.dst}: 守卫 {show(e.guard)} 不能保证 {e.counter} < {hi}，计数器会越界")
            except ExprError as ex:
                msgs.append(f"边 {e.src} → {e.dst}: {ex}")
    return Check("W3", not msgs, msgs)


def _w4(skill) -> Check:
    msgs, I, T = [], skill.interface, skill.types

    # 被调 skill 只有以 done 结束才交付输出，所以调用节点的输出变量
    # 只在"守卫能推出 outcome == done"的出边上才算写过。
    def writes(n):
        return frozenset([n.outcome_var] if n.kind == "call" else n.writes)

    def edge(e, d):
        n = skill.nodes[e.src]
        if n.kind == "call" and entails([e.guard], Bin("==", Var(n.outcome_var), Lit("done")), T) is None:
            return d | frozenset(n.bind_out.values())
        return d

    IN = _forward(skill, set(I.inputs) | skill.counters, lambda v, d: d | writes(skill.nodes[v]), edge)
    for v, n in skill.nodes.items():
        d = IN[v]
        if d is None:
            continue
        for x in n.reads:
            if x not in d:
                msgs.append(f"节点 {v} 读 {x}，但存在一条到达它的路径上 {x} 还没被写过")
        after = d | writes(n)
        for e in skill.out_edges(v):
            for x in sorted(vars_of(e.guard) & set(skill.types)):
                if x not in after:
                    msgs.append(f"边 {v} → {e.dst} 的守卫读 {x}，但它可能还没被写过")
        if n.kind == "terminal" and n.outcome == "done":
            for x in I.outputs:
                if x not in d:
                    msgs.append(f"终点 {v} 以 done 结束，但输出 {x} 可能还没被写过")
    return Check("W4", not msgs, msgs)


def _w5(skill, facts) -> Check:
    msgs, T = [], skill.types
    for inv in skill.interface.inv:
        iv = vars_of(inv) & set(T)
        for v, n in skill.nodes.items():
            if n.kind == "terminal" or not iv & set(n.writes):
                continue
            try:
                cex = entails(facts.post[v], inv, T)
            except ExprError as ex:
                msgs.append(f"节点 {v}: {ex}")
                continue
            if cex is not None:
                what = {"agent": "契约", "tool": "ensures", "call": "被调 skill 的接口"}[n.kind]
                msgs.append(f"节点 {v} 改写了 {sorted(iv & set(n.writes))}，但它的{what}不能保证 {show(inv)}")
    return Check("W5", not msgs, msgs)


def _w6(skill, facts, resolver) -> Check:
    msgs, T = [], skill.types
    for n in skill.calls():
        callee = resolver(n.skill, n.min_version) if resolver else None
        if callee is None:
            msgs.append(f"调用节点 {n.id}: 库里找不到 {n.skill} >= {n.min_version}")
            continue
        ci, bad = callee.interface, False
        for k, t in ci.inputs.items():
            if k not in n.bind_in:
                if k not in ci.defaults:
                    msgs.append(f"调用节点 {n.id}: 没有绑定 {callee.key} 的必填输入 {k}")
                    bad = True
            elif n.bind_in[k] not in T or T[n.bind_in[k]] != t:
                msgs.append(f"调用节点 {n.id}: 输入 {k}: {t} 绑定到了 {n.bind_in[k]}: {T.get(n.bind_in[k], '未声明')}")
                bad = True
        for k in n.bind_in:
            if k not in ci.inputs:
                msgs.append(f"调用节点 {n.id}: {callee.key} 没有输入 {k}")
                bad = True
        for k, x in n.bind_out.items():
            if k not in ci.outputs:
                msgs.append(f"调用节点 {n.id}: {callee.key} 没有输出 {k}")
            elif x not in T or T[x] != ci.outputs[k]:
                msgs.append(f"调用节点 {n.id}: 输出 {k}: {ci.outputs[k]} 写回到了 {x}: {T.get(x, '未声明')}")
        if not bad:
            pre = subst(ci.pre, {k: Var(v) for k, v in n.bind_in.items()})
            msgs += [f"调用节点 {n.id}: 调用方保证不了 {callee.key} 的 Pre；{m}" for m in facts.prove(n.id, pre, T)]
    return Check("W6", not msgs, msgs)


def _w7(skill, resolver) -> Check:
    msgs = []

    def dfs(s, path):
        for n in s.calls():
            if n.skill in path:
                msgs.append(f"调用图有环: {' → '.join(path + [n.skill])}")
                continue
            callee = resolver(n.skill, n.min_version) if resolver else None
            if callee is not None:
                dfs(callee, path + [n.skill])

    dfs(skill, [skill.name])
    return Check("W7", not msgs, msgs)


def llm_bound(skill, resolver, _depth: int = 0) -> int:
    """最坏情况下的模型调用次数上界。

    不在环上的节点至多执行一次；环上的节点至多执行 1 + Σ(该环内各计数器的容量) 次。
    Skill 调用节点的单次代价取被调 skill 声明的预算。
    """
    T, total = skill.types, 0
    for v, n in skill.nodes.items():
        if n.kind == "agent":
            cost = 1
        elif n.kind == "call":
            callee = resolver(n.skill, n.min_version) if resolver else None
            if callee is None or _depth > 50:
                continue
            cost = callee.interface.budget.get("llm_calls", llm_bound(callee, resolver, _depth + 1))
        else:
            continue
        visits, r = 1, _reach(skill, v)
        if v in r:
            scc = {u for u in r if v in _reach(skill, u)} | {v}
            cs = {e.counter for e in skill.edges if e.counter and e.src in scc and e.dst in scc}
            visits += sum(T[c].hi - T[c].lo for c in cs if c in T and T[c].kind == "int")
        total += cost * visits
    return total


def _budget(skill, resolver) -> Check:
    bound, decl = llm_bound(skill, resolver), skill.interface.budget.get("llm_calls")
    if decl is None:
        return Check("B", True, [f"未声明预算；最坏情况需要 {bound} 次模型调用"])
    if bound > decl:
        return Check("B", False, [f"最坏情况需要 {bound} 次模型调用，超过声明的预算 {decl}"])
    return Check("B", True)


def _w8(skill, facts) -> Check:
    msgs, T = [], skill.types
    for v, n in skill.nodes.items():
        if n.kind == "agent":
            msgs += [f"节点 {v} 的前置条件: {m}" for m in facts.prove(v, n.contract.pre, T)]
        elif n.kind == "terminal":
            goal = subst(skill.interface.post_safe, {"outcome": Lit(n.outcome)})
            msgs += [f"终点 {v}: {m}" for m in facts.prove(v, goal, T, f"Post_safe（{show(skill.interface.post_safe)}）")]
    return Check("W8", not msgs, msgs)


def check_skill(skill, resolver=None) -> Report:
    """Gate 0 的良构性部分。resolver(name, min_version) -> Skill | None 用来解析被调 skill。"""
    checks = [_w0(skill)]
    if not checks[0].ok:          # 结构都不对，后面的分析没有意义
        return Report(skill.key, checks)
    facts = _Facts(skill, resolver)
    checks += [_w1(skill), _w2(skill), _w3(skill), _w4(skill), _w5(skill, facts),
               _w6(skill, facts, resolver), _w7(skill, resolver), _budget(skill, resolver)]
    checks.append(_w8(skill, facts))
    return Report(skill.key, checks)
