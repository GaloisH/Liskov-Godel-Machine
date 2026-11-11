"""执行器（设计文档 §7.1）、事后评估（§7.2）与缓存重放（§7.3）。

执行器属于可信内核：它负责算出唯一成立的出边、给计数器加一、检查契约 / 不变量 / 预算、记录轨迹。
Agent 的不确定性全部通过 AgentBackend 接口进来；工具通过名字注册。
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass

from .expr import UNKNOWN, evaluate, show
from .trace import TV, Step, Trace, digest

MAX_STEPS = 10_000      # 内核兜底；通过 W3 的图不会走到这里


class CacheMiss(Exception):
    """重放时遇到缓存里没有的 Agent 输入：分歧前沿。"""


class _Abort(Exception):
    def __init__(self, status, detail=""):
        self.status, self.detail = status, detail


@dataclass
class AgentCall:
    skill: str
    version: str
    node: str
    prompt: str
    view: dict          # 读集对应的黑板内容
    writes: tuple       # 需要产出的变量


class ScriptedBackend:
    """按脚本回放的 Agent 后端，用于 toy example 和测试。

    scripts 的键是 "Skill.node" 或 "node"；值是输出列表（按调用顺序依次取用）
    或函数 view -> 输出 dict。
    """

    def __init__(self, scripts: dict):
        self.scripts, self.calls = scripts, Counter()

    def invoke(self, call: AgentCall) -> dict:
        key = f"{call.skill}.{call.node}"
        if key not in self.scripts:
            key = call.node
        if key not in self.scripts:
            raise KeyError(f"没有为 {call.skill}.{call.node} 提供脚本")
        s, i = self.scripts[key], self.calls[key]
        self.calls[key] += 1
        if callable(s):
            return s(call.view)
        if i >= len(s):
            raise IndexError(f"{key} 的脚本用完了（第 {i + 1} 次调用）")
        return s[i]


class ReplayBackend:
    """用旧轨迹里的 Agent 输出当缓存。键是（prompt 哈希，读集内容哈希）。

    同一个键在旧轨迹里出现多次（比如重试）时，按原来的顺序依次取用——相当于沿用相同的随机种子。
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
    """契约里的谓词。runtime 里的运行时就能算；judge 里的只有事后评估才能用（隐藏测试、标准答案、人工）。

    谓词返回 None 表示判不出；没注册的谓词一律判不出。
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

    # ------------------------------------------------------------ 求值

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

    # ------------------------------------------------------------ 运行

    def run(self, skill, inputs: dict) -> Trace:
        """运行一个 skill（Skill 对象，或库里的名字——取最新版本）。"""
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
                raise ValueError(f"{skill.key}: 缺少输入 {k}")
            if not ty.contains(state[k]):
                raise ValueError(f"{skill.key}: 输入 {k} = {state[k]!r} 不是 {ty}")
        for c in skill.counters:
            state[c] = T[c].lo
        tr.pre = self.tv(I.pre, state, I.types)
        if tr.pre is TV.F:                      # 输入在 Pre 之外：直接拒绝（F0）
            tr.outcome, tr.status, tr.detail = "reject", "pre_violated", show(I.pre)
            return tr
        budget, v = I.budget.get("llm_calls"), skill.entry
        try:
            for t in range(1, MAX_STEPS + 1):
                node = skill.nodes[v]
                if node.kind == "terminal":
                    tr.steps.append(Step(t, v, "terminal"))
                    tr.outcome = node.outcome
                    if node.outcome == "done":      # 只有 Done 才交付输出；放弃或拒绝时不交回半成品
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
                        raise _Abort("type_error", f"{v} 写了写集之外的变量 {k}")
                    if not T[k].contains(val):
                        raise _Abort("type_error", f"{v} 写入 {k} = {val!r}，不是 {T[k]}")
                step.output = out
                state.update(out)
                if node.kind != "call":
                    step.contract_post = self.tv(node.contract.post, {**view, **out}, T)
                step.inv = self._all(self.tv(i, state, T) for i in I.inv)
                if step.inv is TV.F:
                    raise _Abort("inv_violation", f"{v} 之后不变量被违反")
                if budget is not None and tr.llm_calls > budget:
                    raise _Abort("budget_exceeded", f"模型调用 {tr.llm_calls} 次，预算 {budget}")
                live = [e for e in skill.out_edges(v) if self._value(e.guard, state, T) is True]
                if len(live) != 1:
                    raise _Abort("stuck", f"{v} 处有 {len(live)} 条出边成立")
                e = live[0]
                if e.counter:
                    state[e.counter] += 1
                step.next = v = e.dst
            raise _Abort("stuck", "超过内核步数上限")
        except _Abort as a:                     # 内核中止：诚实地放弃，不交付任何输出
            tr.outcome, tr.status, tr.detail = "give_up", a.status, a.detail
            return tr

    def _exec(self, skill, node, view, step, tr, budget) -> dict:
        T = skill.types
        if node.kind == "tool":
            if node.fn not in self.tools:
                raise _Abort("tool_error", f"工具 {node.fn} 未注册")
            try:
                return dict(self.tools[node.fn](**view))
            except Exception as ex:
                raise _Abort("tool_error", f"{node.fn}: {ex!r}") from ex
        if node.kind == "agent":
            if budget is not None and tr.llm_calls + 1 > budget:
                raise _Abort("budget_exceeded", f"调用 {node.id} 会超出预算 {budget}")
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
        # Skill 调用：按绑定传参，运行被调 skill，收回输出和 outcome
        callee = self.library.resolve(node.skill, node.min_version)
        if callee is None:
            raise _Abort("tool_error", f"库里找不到 {node.skill} >= {node.min_version}")
        sub = self._run(callee, {k: view[x] for k, x in node.bind_in.items() if x in view})
        step.sub_trace = sub
        step.cost["llm_calls"] = sub.llm_calls
        if sub.status == "frontier":
            tr.status, tr.frontier = "frontier", sub.frontier
            return {}
        step.contract_pre = sub.pre
        step.contract_post = self._post(callee, sub)["post_safe"]   # 调用节点的契约就是被调 skill 的 Post
        out = {x: sub.outputs[k] for k, x in node.bind_out.items() if k in sub.outputs}
        out[node.outcome_var] = sub.outcome
        return out

    # ------------------------------------------------------------ 事后评估

    def _post(self, skill, tr: Trace, use_judge=False) -> dict:
        I = skill.interface
        env = {**tr.inputs, **tr.outputs, "outcome": tr.outcome}
        return {"post_safe": self.tv(I.post_safe, env, I.types, use_judge),
                "post_live": self.tv(I.post_live, env, I.types, use_judge),
                "inv": self._all(self.tv(i, env, skill.types, use_judge) for i in I.inv)}

    def posthoc(self, tr: Trace) -> Trace:
        """事后评估：用 judge 重新检查每一步的契约，以及整次运行的 Post 是否真的成立。"""
        if tr.status == "frontier":
            raise ValueError("停在分歧前沿的重放没有最终结果，不能做事后评估")
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
        # 不变量是"全程"性质：以运行时逐步检查的结果为准，最终状态上判不出时（比如放弃后没有输出）沿用它
        steps = self._all(s.inv for s in tr.steps if s.inv is not None)
        if steps is TV.F or tr.post_eval["inv"] is TV.U:
            tr.post_eval["inv"] = steps
        return tr

    # ------------------------------------------------------------ 缓存重放

    def replay(self, skill, old: Trace) -> Trace:
        """把（修改后的）skill 放在旧轨迹的输入上重放：Agent 步用缓存，工具步重新算。

        返回的轨迹如果 status == "frontier"，说明停在了第一个需要新 Agent 输出的位置；
        否则整个重放都是精确的反事实结果。
        """
        ex = Executor(self.library, self.tools, ReplayBackend([old]), self.predicates)
        tr = ex.run(skill, old.inputs)
        self._skills.update(ex._skills)
        return tr
