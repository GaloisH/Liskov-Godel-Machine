"""Executor (design doc §7.1), post-hoc evaluation (§7.2) and cached replay (§7.3).

The executor is part of the trusted kernel: it computes the unique enabled outgoing edge, increments counters, checks contracts / invariants / budget, and records the trace.
All agent nondeterminism enters through the AgentBackend interface; tools are registered by name.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass

from .expr import UNKNOWN, evaluate, show
from .trace import TV, Step, Trace, digest

MAX_STEPS = 10_000      # kernel safety net; graphs that pass W3 never get here


class CacheMiss(Exception):
    """An agent input missing from the cache during replay: the divergence frontier."""


class _Abort(Exception):
    def __init__(self, status, detail=""):
        self.status, self.detail = status, detail


@dataclass
class AgentCall:
    skill: str
    version: str
    node: str
    prompt: str
    view: dict          # blackboard contents of the read set
    writes: tuple       # variables to produce


class ScriptedBackend:
    """A scripted agent backend for toy examples and tests.

    Keys of scripts are "Skill.node" or "node"; values are lists of outputs (consumed in call order)
    or functions view -> output dict.
    """

    def __init__(self, scripts: dict):
        self.scripts, self.calls = scripts, Counter()

    def invoke(self, call: AgentCall) -> dict:
        key = f"{call.skill}.{call.node}"
        if key not in self.scripts:
            key = call.node
        if key not in self.scripts:
            raise KeyError(f"no script provided for {call.skill}.{call.node}")
        s, i = self.scripts[key], self.calls[key]
        self.calls[key] += 1
        if callable(s):
            return s(call.view)
        if i >= len(s):
            raise IndexError(f"script for {key} exhausted (call #{i + 1})")
        return s[i]


class ReplayBackend:
    """Uses the agent outputs of an old trace as a cache. The key is (prompt hash, read-set content hash).

    When a key appears several times in the old trace (e.g. retries), entries are consumed in their original order, which amounts to reusing the same random seed.
    """

    def __init__(self, traces):
        self.cache, self.hits = defaultdict(deque), 0
        for tr in traces:
            self._load(tr)

    def _load(self, tr: Trace):
        for s in tr.steps:
            if s.kind == "agent" and s.output:
                self.cache[(s.prompt_hash, s.input_hash)].append(s.output)
            if s.sub_trace:
                self._load(s.sub_trace)

    def invoke(self, call: AgentCall) -> dict:
        q = self.cache.get((digest(call.prompt), digest(call.view)))
        if not q:
            raise CacheMiss(f"{call.skill}.{call.node}")
        self.hits += 1
        return q.popleft()


class Predicates:
    """Predicates used in contracts. Those in runtime can be computed at run time; those in judge are only usable in post-hoc evaluation (hidden tests, reference answers, humans).

    A predicate returning None means undecided; unregistered predicates are always undecided.
    """

    def __init__(self, runtime: dict | None = None, judge: dict | None = None):
        self.runtime = {"len": len, **(runtime or {})}
        self.judge = dict(judge or {})

    def call(self, name: str, args: list, use_judge: bool):
        if any(a is UNKNOWN for a in args):
            return UNKNOWN
        fn = self.runtime.get(name) or (self.judge.get(name) if use_judge else None)
        if fn is None:
            return UNKNOWN
        r = fn(*args)
        return UNKNOWN if r is None else r


class Executor:
    def __init__(self, library, tools: dict, backend, predicates: Predicates | None = None):
        self.library, self.tools, self.backend = library, tools, backend
        self.predicates = predicates or Predicates()
        self._skills = {}

    # ------------------------------------------------------------ evaluation

    def _value(self, expr, env, types, use_judge=False):
        return evaluate(expr, lambda n: env[n] if n in env else (UNKNOWN if n in types else n),
                        lambda node, args: self.predicates.call(node.fn, args, use_judge))

    def tv(self, expr, env, types, use_judge=False) -> TV:
        try:
            return TV.of(self._value(expr, env, types, use_judge))
        except Exception:
            return TV.U

    @staticmethod
    def _all(tvs) -> TV:
        tvs = list(tvs)
        return TV.F if TV.F in tvs else TV.U if TV.U in tvs else TV.T

    # ------------------------------------------------------------ running

    def run(self, skill, inputs: dict) -> Trace:
        """Run a skill (a Skill object, or a name in the library, which resolves to the latest version)."""
        if isinstance(skill, str):
            skill = self.library.resolve(skill)
        return self._run(skill, dict(inputs))

    def _run(self, skill, inputs: dict) -> Trace:
        I, T = skill.interface, skill.types
        self._skills[(skill.name, str(skill.version))] = skill
        tr = Trace(skill.name, str(skill.version), inputs)
        state = {}
        for k, ty in I.inputs.items():
            if k in inputs:
                state[k] = inputs[k]
            elif k in I.defaults:
                state[k] = I.defaults[k]
            else:
                raise ValueError(f"{skill.key}: missing input {k}")
            if not ty.contains(state[k]):
                raise ValueError(f"{skill.key}: input {k} = {state[k]!r} is not a {ty}")
        for c in skill.counters:
            state[c] = T[c].lo
        tr.pre = self.tv(I.pre, state, I.types)
        if tr.pre is TV.F:                      # input outside Pre: reject immediately (F0)
            tr.outcome, tr.status, tr.detail = "reject", "pre_violated", show(I.pre)
            return tr
        budget, v = I.budget.get("llm_calls"), skill.entry
        try:
            for t in range(1, MAX_STEPS + 1):
                node = skill.nodes[v]
                if node.kind == "terminal":
                    tr.steps.append(Step(t, v, "terminal"))
                    tr.outcome = node.outcome
                    if node.outcome == "done":      # only Done delivers outputs; giving up or rejecting hands back no partial results
                        tr.outputs = {k: state[k] for k in I.outputs if k in state}
                    return tr
                view = {k: state[k] for k in node.reads if k in state}
                step = Step(t, v, node.kind, view, digest(view))
                tr.steps.append(step)
                t0 = time.perf_counter()
                out = self._exec(skill, node, view, step, tr, budget)
                step.cost["seconds"] = round(time.perf_counter() - t0, 6)
                if tr.status == "frontier":
                    return tr
                for k, val in out.items():
                    if k not in node.writes:
                        raise _Abort("type_error", f"{v} wrote variable {k} outside its write set")
                    if not T[k].contains(val):
                        raise _Abort("type_error", f"{v} wrote {k} = {val!r}, which is not a {T[k]}")
                step.output = out
                state.update(out)
                if node.kind != "call":
                    step.contract_post = self.tv(node.contract.post, {**view, **out}, T)
                step.inv = self._all(self.tv(i, state, T) for i in I.inv)
                if step.inv is TV.F:
                    raise _Abort("inv_violation", f"invariant violated after {v}")
                if budget is not None and tr.llm_calls > budget:
                    raise _Abort("budget_exceeded", f"{tr.llm_calls} model calls, budget {budget}")
                live = [e for e in skill.out_edges(v) if self._value(e.guard, state, T) is True]
                if len(live) != 1:
                    raise _Abort("stuck", f"{len(live)} outgoing edges enabled at {v}")
                e = live[0]
                if e.counter:
                    state[e.counter] += 1
                step.next = v = e.dst
            raise _Abort("stuck", "kernel step limit exceeded")
        except _Abort as a:                     # kernel abort: give up honestly, deliver no outputs
            tr.outcome, tr.status, tr.detail = "give_up", a.status, a.detail
            return tr

    def _exec(self, skill, node, view, step, tr, budget) -> dict:
        T = skill.types
        if node.kind == "tool":
            if node.fn not in self.tools:
                raise _Abort("tool_error", f"tool {node.fn} is not registered")
            try:
                return dict(self.tools[node.fn](**view))
            except Exception as ex:
                raise _Abort("tool_error", f"{node.fn}: {ex!r}") from ex
        if node.kind == "agent":
            if budget is not None and tr.llm_calls + 1 > budget:
                raise _Abort("budget_exceeded", f"call {node.id} would exceed budget {budget}")
            step.contract_pre = self.tv(node.contract.pre, view, T)
            step.prompt_hash = digest(node.prompt)
            call = AgentCall(skill.name, str(skill.version), node.id, node.prompt, view, node.writes)
            try:
                out = dict(self.backend.invoke(call))
            except CacheMiss:
                tr.status = "frontier"
                tr.frontier = {"skill": skill.name, "node": node.id, "t": step.t, "input_view": view}
                return {}
            step.cost["llm_calls"] = 1
            return out
        # skill call: pass arguments by binding, run the callee, collect outputs and outcome
        callee = self.library.resolve(node.skill, node.min_version)
        if callee is None:
            raise _Abort("tool_error", f"no {node.skill} >= {node.min_version} in the library")
        sub = self._run(callee, {k: view[x] for k, x in node.bind_in.items() if x in view})
        step.sub_trace = sub
        step.cost["llm_calls"] = sub.llm_calls
        if sub.status == "frontier":
            tr.status, tr.frontier = "frontier", sub.frontier
            return {}
        step.contract_pre = sub.pre
        step.contract_post = self._post(callee, sub)["post_safe"]   # the contract of a call node is the callee's Post
        out = {x: sub.outputs[k] for k, x in node.bind_out.items() if k in sub.outputs}
        out[node.outcome_var] = sub.outcome
        return out

    # ------------------------------------------------------------ post-hoc evaluation

    def _post(self, skill, tr: Trace, use_judge=False) -> dict:
        I = skill.interface
        env = {**tr.inputs, **tr.outputs, "outcome": tr.outcome}
        return {"post_safe": self.tv(I.post_safe, env, I.types, use_judge),
                "post_live": self.tv(I.post_live, env, I.types, use_judge),
                "inv": self._all(self.tv(i, env, skill.types, use_judge) for i in I.inv)}

    def posthoc(self, tr: Trace) -> Trace:
        """Post-hoc evaluation: re-check every step's contract with the judge, and whether the Post of the whole run really holds."""
        if tr.status == "frontier":
            raise ValueError("a replay stopped at the divergence frontier has no final result and cannot be evaluated post hoc")
        skill = self._skills.get((tr.skill, tr.version)) or self.library.get(tr.skill, tr.version)
        T = skill.types
        for s in tr.steps:
            node = skill.nodes[s.node]
            if s.sub_trace:
                self.posthoc(s.sub_trace)
                s.contract_post = s.sub_trace.post_eval["post_safe"]
            elif node.kind in ("agent", "tool") and s.output:
                s.contract_post = self.tv(node.contract.post, {**s.input_view, **s.output}, T, True)
                if node.kind == "agent":
                    s.contract_pre = self.tv(node.contract.pre, s.input_view, T, True)
        tr.post_eval = self._post(skill, tr, use_judge=True)
        # invariants are whole-run properties: the step-by-step run-time result is authoritative, and is kept when the final state is undecided (e.g. no outputs after giving up)
        steps = self._all(s.inv for s in tr.steps if s.inv is not None)
        if steps is TV.F or tr.post_eval["inv"] is TV.U:
            tr.post_eval["inv"] = steps
        return tr

    # ------------------------------------------------------------ cached replay

    def replay(self, skill, old: Trace) -> Trace:
        """Replay a (modified) skill on the inputs of an old trace: agent steps come from the cache, tool steps are recomputed.

        If the returned trace has status == "frontier", it stopped at the first point that needs new agent output;
        otherwise the whole replay is an exact counterfactual.
        """
        ex = Executor(self.library, self.tools, ReplayBackend([old]), self.predicates)
        tr = ex.run(skill, old.inputs)
        self._skills.update(ex._skills)
        return tr
