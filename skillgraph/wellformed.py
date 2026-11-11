"""Well-formedness checks W0–W8 and the static budget check (design doc §6). Fully automatic, no agent involved.

W0  structure and types: all references declared, edge conditions read only decidable variables, contracts reference only the node's read/write sets, ...
W1  deterministic and total   W2  reachability   W3  termination   W4  write before read
W5  invariant preservation   W6  call interface match   W7  acyclic call graph
B   budget: the worst-case number of model calls does not exceed the declared budget
W8  spec consistency (optional): if every node keeps its contract, starting from Pre always establishes Post_safe

W5 / W6 / W8 share one forward "fact propagation" data-flow analysis: a node that keeps its contract leaves its postcondition,
every edge leaves its guard; once a variable is overwritten, facts mentioning it are invalidated; facts are intersected at join points.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .expr import Bin, ExprError, Lit, Var, calls_of, conjuncts, show, subst, vars_of
from .logic import _abs_eval, entails, exactly_one, fmt_cex
from .types import OUTCOMES

HARD = ("W0", "W1", "W2", "W3", "W4", "W5", "W6", "W7", "B")
NAMES = {"W0": "structure and types", "W1": "deterministic and total", "W2": "reachability", "W3": "termination",
         "W4": "write before read",
         "W5": "invariant preservation", "W6": "call interface match", "W7": "acyclic call graph",
         "B": "budget", "W8": "spec consistency"}


@dataclass
class Check:
    code: str
    ok: bool
    messages: list = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.code == "W8":
            return "proved" if self.ok else "unproved"
        return "pass" if self.ok else "FAIL"


@dataclass
class Report:
    skill: str
    checks: list

    @property
    def ok(self) -> bool:
        """Do all hard requirements (W0–W7 and budget) pass? W8 does not count."""
        return all(c.ok for c in self.checks if c.code in HARD)

    @property
    def w8(self) -> bool:
        return any(c.code == "W8" and c.ok for c in self.checks)

    def __getitem__(self, code: str) -> Check:
        return next(c for c in self.checks if c.code == code)

    def failed(self) -> list:
        return [c.code for c in self.checks if not c.ok and c.code in HARD]

    def __str__(self) -> str:
        lines = [f"{self.skill}: {'well-formed' if self.ok else 'ill-formed'}"
                 f" (spec consistency {'proved' if self.w8 else 'unproved'})"]
        for c in self.checks:
            lines.append(f"  {c.code:<2} {NAMES[c.code]:<24} {c.status}")
            lines += [f"       - {m}" for m in c.messages]
        return "\n".join(lines)


# ---------------------------------------------------------------- utilities

def _reach(skill, start: str, forward: bool = True) -> set:
    """Nodes reachable from start in at least one step."""
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
    """Forward must analysis: the lattice is "sets + intersection"; unreached nodes are None."""
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
    """Translate the callee's Post_safe and Inv into the caller's variables."""
    m = {k: Var(v) for k, v in {**node.bind_in, **node.bind_out}.items()}
    m["outcome"] = Var(node.outcome_var)
    known = set(callee.interface.types)
    out = []
    for c in conjuncts(callee.interface.post_safe) + [x for i in callee.interface.inv for x in conjuncts(i)]:
        if vars_of(c) & known <= set(m):          # unbound callee variables are invisible to the caller; drop them
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
    """Result of fact propagation: the set of facts holding on every edge and at every node entry."""

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
        """Facts holding on each path to node v (distinguished by incoming edge). More precise than the intersection at the entry."""
        out = [("entry", self.init)] if v == self.skill.entry else []
        for e in self.skill.in_edges(v):
            if self.IN[e.src] is not None:
                out.append((f"{e.src} → {v}", self._edge(e, self._node(e.src, self.IN[e.src]))))
        return out

    def prove(self, v, goal, types, shown=None) -> list:
        """Prove goal on every path reaching v. Returns the paths where it cannot be proved, with counterexamples."""
        fails, shown = [], shown or show(goal)
        for label, facts in self.arriving(v):
            try:
                cex = entails(list(facts), goal, types)
            except ExprError as ex:
                fails.append(f"via {label}: {ex}")
                continue
            if cex is not None:
                fails.append(f"via {label}: cannot prove {shown}; counterexample: {fmt_cex(cex)}")
        return fails


# ---------------------------------------------------------------- checks

def _w0(skill) -> Check:
    msgs, T, I = [], skill.types, skill.interface
    syms = {v for t in T.values() for v in t.values} | set(OUTCOMES)

    def chk(e, allowed, where, guard=False, universe=T):
        for n in sorted(vars_of(e)):
            if n in universe:
                if n not in allowed:
                    msgs.append(f"{where}: may not reference variable {n}")
                elif guard and not universe[n].decidable:
                    msgs.append(f"{where}: edge conditions may only read decidable types, {n} is {universe[n]}")
            elif n not in syms:
                msgs.append(f"{where}: undeclared identifier {n}")
        if guard and calls_of(e):
            msgs.append(f"{where}: edge conditions may not call predicates {sorted(calls_of(e))}; have an agent node output an enum value instead")

    if skill.entry not in skill.nodes:
        msgs.append(f"entry node {skill.entry} does not exist")
    for v, n in skill.nodes.items():
        for x in (*n.reads, *n.writes):
            if x not in T:
                msgs.append(f"node {v}: variable {x} in the read/write set is undeclared")
        for x in n.writes:
            if x in I.inputs:
                msgs.append(f"node {v}: may not overwrite input variable {x}")
            if x in skill.counters:
                msgs.append(f"node {v}: may not write loop counter {x} (counters are only incremented by the executor on back edges)")
        outs = skill.out_edges(v)
        if n.kind == "terminal" and outs:
            msgs.append(f"terminal {v} may not have outgoing edges")
        if n.kind != "terminal" and not outs:
            msgs.append(f"node {v} has no outgoing edges")
        if n.kind in ("agent", "tool"):
            chk(n.contract.pre, set(n.reads), f"precondition of node {v}")
            chk(n.contract.post, set(n.reads) | set(n.writes), f"postcondition of node {v}")
    for e in skill.edges:
        for x in (e.src, e.dst):
            if x not in skill.nodes:
                msgs.append(f"edge {e.src} → {e.dst}: node {x} does not exist")
        chk(e.guard, set(T), f"edge {e.src} → {e.dst}", guard=True)
        if e.counter and (e.counter not in T or T[e.counter].kind != "int" or e.counter in I.inputs):
            msgs.append(f"edge {e.src} → {e.dst}: counter {e.counter} must be a non-input bounded integer")
    for x in I.outputs:
        if x in I.inputs:
            msgs.append(f"variable {x} cannot be both an input and an output")
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
            msgs.append(f"node {v}: {ex}")
            continue
        if cex is not None:
            env = {k: val for k, val in cex.items() if k in T}
            hit = [show(g) for g in guards if _abs_eval(g, env, T)]
            what = "no outgoing edge is enabled (stuck)" if not hit else f"{len(hit)} outgoing edges are enabled at once: {hit}"
            msgs.append(f"node {v}: in state {fmt_cex(env)}, {what}")
    return Check("W1", not msgs, msgs)


def _w2(skill) -> Check:
    msgs = []
    fwd = _reach(skill, skill.entry) | {skill.entry}
    terms = [v for v, n in skill.nodes.items() if n.kind == "terminal"]
    if not terms:
        msgs.append("no terminal")
    back = set(terms)
    for t in terms:
        back |= _reach(skill, t, forward=False)
    for v in skill.nodes:
        if v not in fwd:
            msgs.append(f"node {v} is unreachable from the entry")
        elif v not in back:
            msgs.append(f"node {v} cannot reach any terminal")
    return Check("W2", not msgs, msgs)


def _w3(skill) -> Check:
    msgs, T = [], skill.types
    # after removing counter edges, the graph must be acyclic
    plain = {v: [e.dst for e in skill.out_edges(v) if not e.counter] for v in skill.nodes}
    state = {}

    def dfs(v, path):
        state[v] = 1
        for w in plain.get(v, ()):
            if state.get(w) == 1:
                msgs.append(f"cycle {' → '.join(path[path.index(w):] + [w])} has no counter edge")
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
                    msgs.append(f"edge {e.src} → {e.dst}: guard {show(e.guard)} does not ensure {e.counter} < {hi}; the counter can overflow")
            except ExprError as ex:
                msgs.append(f"edge {e.src} → {e.dst}: {ex}")
    return Check("W3", not msgs, msgs)


def _w4(skill) -> Check:
    msgs, I, T = [], skill.interface, skill.types

    # a callee only delivers outputs when it ends with done, so the output variables of a call node
    # count as written only on outgoing edges whose guard implies outcome == done.
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
                msgs.append(f"node {v} reads {x}, but {x} is unwritten on some path reaching it")
        after = d | writes(n)
        for e in skill.out_edges(v):
            for x in sorted(vars_of(e.guard) & set(skill.types)):
                if x not in after:
                    msgs.append(f"the guard of edge {v} → {e.dst} reads {x}, which may be unwritten")
        if n.kind == "terminal" and n.outcome == "done":
            for x in I.outputs:
                if x not in d:
                    msgs.append(f"terminal {v} ends with done, but output {x} may be unwritten")
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
                msgs.append(f"node {v}: {ex}")
                continue
            if cex is not None:
                what = {"agent": "contract", "tool": "ensures", "call": "callee interface"}[n.kind]
                msgs.append(f"node {v} writes {sorted(iv & set(n.writes))}, but its {what} does not ensure {show(inv)}")
    return Check("W5", not msgs, msgs)


def _w6(skill, facts, resolver) -> Check:
    msgs, T = [], skill.types
    for n in skill.calls():
        callee = resolver(n.skill, n.min_version) if resolver else None
        if callee is None:
            msgs.append(f"call node {n.id}: no {n.skill} >= {n.min_version} in the library")
            continue
        ci, bad = callee.interface, False
        for k, t in ci.inputs.items():
            if k not in n.bind_in:
                if k not in ci.defaults:
                    msgs.append(f"call node {n.id}: required input {k} of {callee.key} is not bound")
                    bad = True
            elif n.bind_in[k] not in T or T[n.bind_in[k]] != t:
                msgs.append(f"call node {n.id}: input {k}: {t} is bound to {n.bind_in[k]}: {T.get(n.bind_in[k], 'undeclared')}")
                bad = True
        for k in n.bind_in:
            if k not in ci.inputs:
                msgs.append(f"call node {n.id}: {callee.key} has no input {k}")
                bad = True
        for k, x in n.bind_out.items():
            if k not in ci.outputs:
                msgs.append(f"call node {n.id}: {callee.key} has no output {k}")
            elif x not in T or T[x] != ci.outputs[k]:
                msgs.append(f"call node {n.id}: output {k}: {ci.outputs[k]} is written back to {x}: {T.get(x, 'undeclared')}")
        if not bad:
            pre = subst(ci.pre, {k: Var(v) for k, v in n.bind_in.items()})
            msgs += [f"call node {n.id}: the caller cannot guarantee the Pre of {callee.key}; {m}" for m in facts.prove(n.id, pre, T)]
    return Check("W6", not msgs, msgs)


def _w7(skill, resolver) -> Check:
    msgs = []

    def dfs(s, path):
        for n in s.calls():
            if n.skill in path:
                msgs.append(f"cycle in the call graph: {' → '.join(path + [n.skill])}")
                continue
            callee = resolver(n.skill, n.min_version) if resolver else None
            if callee is not None:
                dfs(callee, path + [n.skill])

    dfs(skill, [skill.name])
    return Check("W7", not msgs, msgs)


def llm_bound(skill, resolver, _depth: int = 0) -> int:
    """Upper bound on the worst-case number of model calls.

    A node not on a cycle runs at most once; a node on a cycle runs at most 1 + Σ(capacities of the counters in that cycle) times.
    The per-call cost of a skill call node is the budget declared by the callee.
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
        return Check("B", True, [f"no budget declared; worst case needs {bound} model calls"])
    if bound > decl:
        return Check("B", False, [f"worst case needs {bound} model calls, exceeding the declared budget {decl}"])
    return Check("B", True)


def _w8(skill, facts) -> Check:
    msgs, T = [], skill.types
    for v, n in skill.nodes.items():
        if n.kind == "agent":
            msgs += [f"precondition of node {v}: {m}" for m in facts.prove(v, n.contract.pre, T)]
        elif n.kind == "terminal":
            goal = subst(skill.interface.post_safe, {"outcome": Lit(n.outcome)})
            msgs += [f"terminal {v}: {m}" for m in facts.prove(v, goal, T, f"Post_safe ({show(skill.interface.post_safe)})")]
    return Check("W8", not msgs, msgs)


def check_skill(skill, resolver=None) -> Report:
    """The well-formedness part of Gate 0. resolver(name, min_version) -> Skill | None resolves callees."""
    checks = [_w0(skill)]
    if not checks[0].ok:          # structure is wrong; the remaining analyses are meaningless
        return Report(skill.key, checks)
    facts = _Facts(skill, resolver)
    checks += [_w1(skill), _w2(skill), _w3(skill), _w4(skill), _w5(skill, facts),
               _w6(skill, facts, resolver), _w7(skill, resolver), _budget(skill, resolver)]
    checks.append(_w8(skill, facts))
    return Report(skill.key, checks)
